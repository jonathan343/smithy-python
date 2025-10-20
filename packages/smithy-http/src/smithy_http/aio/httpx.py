#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncGenerator, AsyncIterable
from copy import copy, deepcopy
from itertools import chain
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    # pyright doesn't like optional imports. This is reasonable because if we use these
    # in type hints then they'd result in runtime errors.
    # TODO: add integ tests that import these without the dependendency installed
    import httpx

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False  # type: ignore

from smithy_core.aio.interfaces import StreamingBlob
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.exceptions import MissingDependencyError
from smithy_core.interfaces import URI

from .. import Field, Fields
from ..interfaces import (
    FieldPosition,
    HTTPClientConfiguration,
    HTTPRequestConfiguration,
)
from . import interfaces as http_aio_interfaces


def _assert_httpx() -> None:
    if not HAS_HTTPX:
        raise MissingDependencyError(
            "Attempted to use httpx component, but httpx is not installed."
        )


class HTTPXHTTPResponse(http_aio_interfaces.HTTPResponse):
    def __init__(
        self,
        *,
        status: int,
        fields: Fields,
        response: "httpx.Response",
    ) -> None:
        _assert_httpx()
        self._status = status
        self._fields = fields
        self._response = response

    @property
    def status(self) -> int:
        return self._status

    @property
    def fields(self) -> Fields:
        return self._fields

    @property
    def body(self) -> AsyncIterable[bytes]:
        return self.chunks()

    @property
    def reason(self) -> str | None:
        """Optional string provided by the server explaining the status."""
        return self._response.reason_phrase

    async def chunks(self) -> AsyncGenerator[bytes, None]:
        async for chunk in self._response.aiter_bytes():
            yield chunk

    def __repr__(self) -> str:
        return (
            f"HTTPXHTTPResponse(status={self.status}, fields={self.fields!r}, body=...)"
        )


class HTTPXClientConfig(HTTPClientConfiguration):
    def __post_init__(self) -> None:
        _assert_httpx()


class HTTPXClient(http_aio_interfaces.HTTPClient):
    """Implementation of :py:class:`.interfaces.HTTPClient` using httpx."""

    def __init__(
        self,
        *,
        client_config: HTTPXClientConfig | None = None,
        _client: "httpx.AsyncClient | None" = None,
    ) -> None:
        """
        :param client_config: Configuration that applies to all requests made with this
        client.
        """
        _assert_httpx()
        self._config = client_config or HTTPXClientConfig()
        self._client = _client or httpx.AsyncClient(http2=True)

    async def send(
        self,
        request: http_aio_interfaces.HTTPRequest,
        *,
        request_config: http_aio_interfaces.HTTPRequestConfiguration | None = None,
    ) -> HTTPXHTTPResponse:
        """Send HTTP request using httpx client.

        :param request: The request including destination URI, fields, payload.
        :param request_config: Configuration specific to this request.
        """
        request_config = request_config or HTTPRequestConfiguration()

        headers_list = list(
            chain.from_iterable(fld.as_tuples() for fld in request.fields)
        )

        # Convert body to async generator for request_body_generator
        body_generator = self._create_body_generator(request.body)

        # Use stream=True to enable response streaming
        resp = await self._client.request(
            method=request.method,
            url=self._serialize_uri_without_query(request.destination),
            params=parse_qs(request.destination.query),  # type: ignore
            headers=headers_list,
            content=body_generator,
        )
        return await self._marshal_response(resp)

    def _serialize_uri_without_query(self, uri: URI) -> httpx.URL:
        """Serialize all parts of the URI up to and including the path."""
        return httpx.URL(
            scheme=uri.scheme or "",
            host=uri.host,
            port=uri.port,
            username=uri.username,
            password=uri.password,
            path=uri.path or "",
        )

    async def _marshal_response(
        self, httpx_resp: "httpx.Response"
    ) -> HTTPXHTTPResponse:
        """Convert a ``httpx.Response`` to a ``HTTPXHTTPResponse``"""
        headers = Fields()
        for header_name, header_val in httpx_resp.headers.items():
            try:
                headers[header_name].add(header_val)
            except KeyError:
                headers[header_name] = Field(
                    name=header_name,
                    values=[header_val],
                    kind=FieldPosition.HEADER,
                )

        return HTTPXHTTPResponse(
            status=httpx_resp.status_code,
            fields=headers,
            response=httpx_resp,
        )

    async def _create_body_generator(
        self, body: StreamingBlob
    ) -> AsyncGenerator[bytes, None]:
        """Convert various body types to async generator for content parameter."""
        if isinstance(body, bytes):
            # Yield the entire body as a single chunk
            yield body
        elif isinstance(body, bytearray):
            # Convert bytearray to bytes
            yield bytes(body)
        elif isinstance(body, AsyncIterable):
            # Already async iterable, just yield from it
            async for chunk in body:
                if isinstance(chunk, bytearray):
                    yield bytes(chunk)
                else:
                    yield chunk
        else:
            # Assume it's a sync BytesReader, wrap it in AsyncBytesReader
            async_reader = AsyncBytesReader(body)
            async for chunk in async_reader:
                if isinstance(chunk, bytearray):
                    yield bytes(chunk)
                else:
                    yield chunk

    def __deepcopy__(self, memo: Any) -> "HTTPXClient":
        return HTTPXClient(
            client_config=deepcopy(self._config),
            _client=copy(self._client),
        )
