#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
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
from smithy_core.aio.utils import async_list
from smithy_core.exceptions import MissingDependencyError
from smithy_core.interfaces import URI

from .. import Field, Fields
from ..interfaces import (
    FieldPosition,
    HTTPClientConfiguration,
    HTTPRequestConfiguration,
)
from . import HTTPResponse
from .interfaces import HTTPClient, HTTPRequest
from .interfaces import HTTPResponse as HTTPResponseInterface


def _assert_httpx() -> None:
    if not HAS_HTTPX:
        raise MissingDependencyError(
            "Attempted to use httpx component, but httpx is not installed."
        )


class HTTPXClientConfig(HTTPClientConfiguration):
    def __post_init__(self) -> None:
        _assert_httpx()


class HTTPXClient(HTTPClient):
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
        request: HTTPRequest,
        *,
        request_config: HTTPRequestConfiguration | None = None,
    ) -> HTTPResponseInterface:
        """Send HTTP request using httpx client.

        :param request: The request including destination URI, fields, payload.
        :param request_config: Configuration specific to this request.
        """
        request_config = request_config or HTTPRequestConfiguration()

        headers_list = list(
            chain.from_iterable(fld.as_tuples() for fld in request.fields)
        )

        body: StreamingBlob = request.body
        if not isinstance(body, AsyncBytesReader):
            body = AsyncBytesReader(body)

        # The typing on `params` is incorrect, it'll happily accept a mapping whose
        # values are lists (or tuples) and produce expected values.
        # See: https://github.com/aio-libs/aiohttp/issues/8563
        resp = await self._client.request(
            method=request.method,
            url=self._serialize_uri_without_query(request.destination),
            params=parse_qs(request.destination.query),  # type: ignore
            headers=headers_list,
            content=body,
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
    ) -> HTTPResponseInterface:
        """Convert a ``httpx.Response`` to a ``smithy_http.aio.HTTPResponse``"""
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

        return HTTPResponse(
            status=httpx_resp.status_code,
            fields=headers,
            body=async_list([await httpx_resp.aread()]),
            reason=httpx_resp.reason_phrase,
        )

    def __deepcopy__(self, memo: Any) -> "HTTPXClient":
        return HTTPXClient(
            client_config=deepcopy(self._config),
            _client=copy(self._client),
        )
