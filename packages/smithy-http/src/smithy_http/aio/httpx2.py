#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""An asynchronous HTTP client built on httpx2.

The client drives httpx2's transport layer directly rather than
``httpx2.AsyncClient``, which would add cookie persistence, default headers, auth,
and redirect handling that a Smithy client does not want between it and the wire.
"""

from collections.abc import AsyncIterable, AsyncIterator
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    # pyright doesn't like optional imports.
    import httpx2

try:
    import httpx2

    HAS_HTTPX2 = True
except ImportError:
    HAS_HTTPX2 = False  # type: ignore

from smithy_core.aio.types import AsyncBytesReader
from smithy_core.aio.utils import read_streaming_blob_async
from smithy_core.exceptions import ClientTimeoutError

from smithy_http import HTTPResponse
from smithy_http.interfaces import HTTPRequest
from smithy_http.interfaces import HTTPResponse as HTTPResponseInterface

from ..exceptions import SmithyHTTPError
from ..httpx2 import (
    HTTPX2ClientConfig,
    _assert_httpx2,  # pyright: ignore[reportPrivateUsage]
    _extensions,  # pyright: ignore[reportPrivateUsage]
    _headers,  # pyright: ignore[reportPrivateUsage]
    _is_framed,  # pyright: ignore[reportPrivateUsage]
    _origin,  # pyright: ignore[reportPrivateUsage]
    _response_fields,  # pyright: ignore[reportPrivateUsage]
    _transport_kwargs,  # pyright: ignore[reportPrivateUsage]
)
from ..interfaces import HTTPRequestConfiguration
from .interfaces import AsyncHTTPClient

__all__ = ["HAS_HTTPX2", "AsyncHTTPX2Client", "HTTPX2ClientConfig"]

_UPLOAD_CHUNK_SIZE = 128 * 1024


class AsyncHTTPX2Client(AsyncHTTPClient):
    """Implementation of :py:class:`.interfaces.AsyncHTTPClient` using httpx2."""

    TIMEOUT_EXCEPTIONS: tuple[type[Exception], ...] = (
        (httpx2.TimeoutException,) if HAS_HTTPX2 else ()
    )

    # httpx2 speaks HTTP/2, but sends the whole request before it reads any of the
    # response, so it can't interleave request and response data.
    SUPPORTS_DUPLEX_STREAMING = False

    def __init__(
        self,
        *,
        client_config: HTTPX2ClientConfig | None = None,
        _transport: "httpx2.AsyncBaseTransport | None" = None,
    ) -> None:
        """
        :param client_config: Configuration that applies to all requests made with this
        client.
        """
        _assert_httpx2()
        self._config = client_config or HTTPX2ClientConfig()
        self._closed = False
        self._transport = _transport or httpx2.AsyncHTTPTransport(
            **_transport_kwargs(self._config)
        )

    async def __aenter__(self) -> Self:
        if self._closed:
            raise SmithyHTTPError("Cannot enter an HTTP client that has been closed.")
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()

    def __deepcopy__(self, memo: Any) -> Self:
        return self

    async def send(
        self,
        request: HTTPRequest,
        *,
        request_config: HTTPRequestConfiguration | None = None,
    ) -> HTTPResponseInterface:
        """Send HTTP request using httpx2.

        :param request: The request including destination URI, fields, payload.
        :param request_config: Configuration specific to this request.
        """
        if self._closed:
            raise SmithyHTTPError(
                "Cannot send a request after the HTTP client has been closed."
            )
        httpx_request = httpx2.Request(
            method=request.method,
            url=_origin(request.destination),
            headers=_headers(request.fields),
            content=await _request_body(request),
            extensions=_extensions(request.destination, self._config, request_config),
        )
        raw = await self._transport.handle_async_request(httpx_request)
        raw.request = httpx_request
        try:
            return HTTPResponse(
                status=raw.status_code,
                fields=_response_fields(raw),
                body=_AsyncHTTPX2ResponseBody(raw),
                reason=raw.reason_phrase or None,
            )
        except BaseException:
            await raw.aclose()
            raise

    async def close(self) -> None:
        """Close the underlying connection pool."""
        if self._closed:
            return
        self._closed = True
        await self._transport.aclose()


async def _request_body(request: HTTPRequest) -> bytes | AsyncIterable[bytes]:
    """The body to give httpx2, streamed when its framing is already known.

    An async iterable that isn't a reader is passed through, so each item is sent
    as soon as it's produced. Other bodies are read in 128 KiB chunks. A body
    without a Content-Length or chunked transfer encoding is read into memory and
    sent with its length, since services such as S3 reject the chunked encoding
    httpx2 would otherwise pick.
    """
    body = request.body
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if not _is_framed(request.fields):
        return await read_streaming_blob_async(body)
    if not hasattr(body, "read") and isinstance(body, AsyncIterable):
        return body  # pyright: ignore[reportUnknownVariableType]
    return AsyncBytesReader(body).iter_chunks(_UPLOAD_CHUNK_SIZE)


class _AsyncHTTPX2ResponseBody(AsyncIterable[bytes]):
    """Streams a response body, releasing its connection once it is done."""

    def __init__(self, response: "httpx2.Response") -> None:
        self._response = response
        self._reader: AsyncBytesReader | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter_raw()

    async def read(self, size: int = -1) -> bytes:
        if self._reader is None:
            self._reader = AsyncBytesReader(self._iter_raw())
        return await self._reader.read(size)

    async def close(self) -> None:
        # Pools the connection if the body was read to completion, closes it if not.
        await self._response.aclose()

    async def _iter_raw(self) -> AsyncIterator[bytes]:
        # Raw bytes, since the client asks for and decodes encodings itself.
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        except AsyncHTTPX2Client.TIMEOUT_EXCEPTIONS as e:
            raise ClientTimeoutError(
                message=f"A timeout error occurred ({type(e).__name__}: {e})."
            ) from e
        except httpx2.TransportError as e:
            raise SmithyHTTPError(f"Failed to read the response body: {e}") from e
