#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterable, AsyncIterator
from copy import copy, deepcopy
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, Any, Self, cast

if TYPE_CHECKING:
    # pyright doesn't like optional imports. This is reasonable because if we use these
    # in type hints then they'd result in runtime errors.
    # TODO: add integ tests that import these without the dependendency installed
    import aiohttp
    import yarl

try:
    import aiohttp
    import yarl

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False  # type: ignore

from smithy_core.aio.interfaces import StreamingBlob
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.exceptions import ClientTimeoutError, MissingDependencyError
from smithy_core.interfaces import URI

from smithy_http import HTTPResponse
from smithy_http.interfaces import HTTPRequest
from smithy_http.interfaces import HTTPResponse as HTTPResponseInterface

from .. import tuples_to_fields
from .._settings import read_timeout
from ..exceptions import SmithyHTTPError
from ..interfaces import (
    HTTPClientConfiguration,
    HTTPRequestConfiguration,
)
from .interfaces import AsyncHTTPClient

__all__ = ["HAS_AIOHTTP", "AIOHTTPClient", "AIOHTTPClientConfig"]

_UPLOAD_CHUNK_SIZE = 128 * 1024


@dataclass(kw_only=True)
class AIOHTTPClientConfig(HTTPClientConfiguration):
    """Configuration for the aiohttp HTTP client.

    :param connect_timeout: How long, in seconds, to wait for a connection, or None
        to wait indefinitely.
    """

    connect_timeout: float | None = 30.0

    def __post_init__(self) -> None:
        _assert_aiohttp()


class AIOHTTPClient(AsyncHTTPClient):
    """Implementation of :py:class:`.interfaces.AsyncHTTPClient` using aiohttp."""

    TIMEOUT_EXCEPTIONS: tuple[type[Exception], ...] = (TimeoutError,)

    # aiohttp has no HTTP/2 support, so it can never interleave request and
    # response data.
    SUPPORTS_DUPLEX_STREAMING = False

    def __init__(
        self,
        *,
        client_config: AIOHTTPClientConfig | None = None,
        _session: "aiohttp.ClientSession | None" = None,
    ) -> None:
        """
        :param client_config: Configuration that applies to all requests made with this
        client.
        """
        _assert_aiohttp()
        self._config = client_config or AIOHTTPClientConfig()
        self._closed = False
        # Disable transparent response decompression and advertise
        # 'identity' to request uncompressed responses.
        # TODO: add a functional test once the test client framework exists
        self._session = _session or aiohttp.ClientSession(
            auto_decompress=False,
            headers={"Accept-Encoding": "identity"},
            timeout=self._timeout(None),
        )

    async def __aenter__(self) -> Self:
        if self._closed:
            raise SmithyHTTPError("Cannot enter an HTTP client that has been closed.")
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()

    def __deepcopy__(self, memo: Any) -> "AIOHTTPClient":
        return AIOHTTPClient(
            client_config=deepcopy(self._config),
            _session=copy(self._session),
        )

    async def send(
        self,
        request: HTTPRequest,
        *,
        request_config: HTTPRequestConfiguration | None = None,
    ) -> HTTPResponseInterface:
        """Send HTTP request using aiohttp client.

        :param request: The request including destination URI, fields, payload.
        :param request_config: Configuration specific to this request.
        """
        if self._closed:
            raise SmithyHTTPError(
                "Cannot send a request after the HTTP client has been closed."
            )

        # aiohttp chunks a body itself when asked with chunked=True, and rejects a
        # Transfer-Encoding: chunked header, so the header becomes that flag.
        chunked = "transfer-encoding" in request.fields and [
            value.strip().lower()
            for value in request.fields["transfer-encoding"].values
        ] == ["chunked"]
        # aiohttp can't send request trailers, and a trailer is never a header.
        headers_list = list(
            chain.from_iterable(
                fld.as_tuples()
                for fld in request.fields.get_by_type("header")
                if not (chunked and fld.name.lower() == "transfer-encoding")
            )
        )

        body: StreamingBlob | None = request.body
        if (
            "content-length" not in request.fields
            and "transfer-encoding" not in request.fields
        ):
            body = await self._prepare_body(body)
        elif not isinstance(body, AsyncBytesReader):
            body = AsyncBytesReader(body)

        resp = await self._session.request(
            method=request.method,
            url=self._serialize_uri(request.destination),
            headers=headers_list,
            data=_upload_data(request.body, body),
            chunked=True if chunked else None,
            allow_redirects=False,
            skip_auto_headers={"Content-Type"},
            # Set on every request, so a session passed in has no total deadline
            # either.
            timeout=self._timeout(request_config),
        )
        try:
            return self._marshal_response(resp)
        except BaseException:
            resp.release()
            raise

    async def close(self) -> None:
        """Close the underlying aiohttp session and its connection pool."""
        if self._closed:
            return
        self._closed = True
        await self._session.close()

    def _timeout(
        self, request_config: HTTPRequestConfiguration | None
    ) -> "aiohttp.ClientTimeout":
        """Bound connecting and each read, but not the whole request.

        A total deadline would end a long download or event stream that is still
        receiving data.
        """
        return aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._config.connect_timeout,
            sock_read=read_timeout(request_config, None),
        )

    async def _prepare_body(self, body: StreamingBlob) -> AsyncBytesReader | None:
        """Convert a body for aiohttp, omitting seekable bodies with no data."""
        if not isinstance(body, AsyncBytesReader):
            body = AsyncBytesReader(body)

        if not body.seekable():
            return body

        position = await body.seek(0, 1)
        end = await body.seek(0, 2)
        await body.seek(position)
        return None if position == end else body

    def _serialize_uri(self, uri: URI) -> "yarl.URL":
        """Serialize the URI, preserving the already-encoded query string as-is."""
        return yarl.URL.build(
            scheme=uri.scheme or "",
            host=uri.host,
            port=uri.port,
            user=uri.username,
            password=uri.password,
            path=uri.path or "",
            query_string=uri.query or "",
            encoded=True,
        )

    def _marshal_response(
        self, aiohttp_resp: "aiohttp.ClientResponse"
    ) -> HTTPResponseInterface:
        """Convert a ``aiohttp.ClientResponse`` to a ``smithy_http.aio.HTTPResponse``"""
        return HTTPResponse(
            status=aiohttp_resp.status,
            fields=tuples_to_fields(aiohttp_resp.headers.items()),
            body=_AIOHTTPStreamingBody(aiohttp_resp),
            reason=aiohttp_resp.reason,
        )


def _upload_data(source: Any, body: AsyncBytesReader | None) -> Any:
    """What aiohttp uploads for a request body, which it iterates.

    A reader is read in 128 KiB chunks, which uploads faster than the 64 KiB that
    AsyncBytesReader iterates in by default. An async iterable that isn't a reader
    is passed through, so each item is sent as soon as it's produced rather than
    held until a chunk fills.
    """
    if body is None:
        return None
    if not hasattr(source, "read") and isinstance(source, AsyncIterable):
        return cast(AsyncIterable[bytes], source)
    return body.iter_chunks(_UPLOAD_CHUNK_SIZE)


def _assert_aiohttp() -> None:
    if not HAS_AIOHTTP:
        raise MissingDependencyError(
            "Attempted to use aiohttp component, but aiohttp is not installed."
        )


class _AIOHTTPStreamingBody(AsyncIterable[bytes]):
    """Streams a response body, releasing the response once it is done."""

    def __init__(self, response: "aiohttp.ClientResponse") -> None:
        self._response = response
        self._reader: AsyncBytesReader | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def read(self, size: int = -1) -> bytes:
        if self._reader is None:
            self._reader = AsyncBytesReader(self._chunks())
        return await self._reader.read(size)

    async def close(self) -> None:
        # Pools the connection if the body was read to completion, closes it if not.
        self._response.release()

    async def _chunks(self) -> AsyncIterator[bytes]:
        # The reader iterates by line, so use iter_any to get raw chunks.
        try:
            async for chunk in self._response.content.iter_any():
                yield chunk
        except TimeoutError as e:
            # Raised as the client raises timeouts while sending, so it's retried
            # while the client reads an error or non-streaming response.
            raise ClientTimeoutError(
                message=f"A timeout error occurred ({type(e).__name__}: {e})."
            ) from e
