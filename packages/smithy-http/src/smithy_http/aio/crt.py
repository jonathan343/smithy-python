#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#  pyright: reportMissingTypeStubs=false,reportUnknownMemberType=false
from asyncio import CancelledError, Future, ensure_future, gather, shield, wait
from collections.abc import AsyncGenerator, AsyncIterable, Coroutine
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from inspect import iscoroutinefunction
from io import BytesIO
from typing import TYPE_CHECKING, Any, Self, cast

from awscrt.exceptions import AwsCrtError

if TYPE_CHECKING:
    # pyright doesn't like optional imports. This is reasonable because if we use these
    # in type hints then they'd result in runtime errors.
    # TODO: add integ tests that import these without the dependendency installed
    from awscrt import http as crt_http
    from awscrt import io as crt_io
    from awscrt.aio.http import (
        AIOHttpClientConnectionUnified,
        AIOHttpClientStreamUnified,
    )

try:
    from awscrt import http as crt_http
    from awscrt import io as crt_io
    from awscrt.aio.http import (
        AIOHttpClientConnectionUnified,
        AIOHttpClientStreamUnified,
    )

    HAS_CRT = True
except ImportError:
    HAS_CRT = False  # type: ignore

from smithy_core import interfaces as core_interfaces
from smithy_core.aio import interfaces as core_aio_interfaces
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.aio.utils import read_streaming_blob_async
from smithy_core.exceptions import ClientTimeoutError, MissingDependencyError

from .. import Field, Fields, tuples_to_fields
from .. import _settings as http_settings
from .. import interfaces as http_interfaces
from ..exceptions import SmithyHTTPError
from . import interfaces as http_aio_interfaces

__all__ = [
    "DEFAULT_READ_BUFFER_SIZE",
    "HAS_CRT",
    "AWSCRTHTTPClient",
    "AWSCRTHTTPClientConfig",
    "AsyncAWSCRTHTTPResponse",
    "ConnectionPoolDict",
    "ConnectionPoolKey",
]

# Default buffer size for reading from streams (8 KB)
DEFAULT_READ_BUFFER_SIZE = 8192
"""Default number of bytes read from a request body at a time."""

_MAX_TIMEOUT_MS = 2**31 - 1


class _AWSCRTEventLoop:
    def __init__(self) -> None:
        _assert_crt()
        event_loop_group = crt_io.EventLoopGroup(1)
        host_resolver = crt_io.DefaultHostResolver(event_loop_group)
        self._bootstrap = crt_io.ClientBootstrap(event_loop_group, host_resolver)


class AsyncAWSCRTHTTPResponse(http_aio_interfaces.HTTPResponse):
    """HTTP response from the AWS CRT HTTP client."""

    def __init__(
        self,
        *,
        status: int,
        fields: Fields,
        stream: "AIOHttpClientStreamUnified",
        read_timeout: float | None = None,
        connection: "AIOHttpClientConnectionUnified | None" = None,
    ) -> None:
        """
        :param status: The response's status code.
        :param fields: The response's header fields.
        :param stream: The stream the body arrives on.
        :param read_timeout: How long, in seconds, to wait for each chunk of the
            body, or None to wait indefinitely.
        :param connection: The stream's connection, which is closed if a read times
            out or is cancelled, since the CRT can't cancel one stream. Without it,
            reads aren't bounded.
        """
        _assert_crt()
        self._status = status
        self._fields = fields
        self._stream = stream
        self._read_timeout = read_timeout
        self._connection = connection
        self._body = AsyncBytesReader(self.chunks())

    def __repr__(self) -> str:
        return (
            f"AsyncAWSCRTHTTPResponse("
            f"status={self.status}, "
            f"fields={self.fields!r}, body=...)"
        )

    @property
    def status(self) -> int:
        return self._status

    @property
    def fields(self) -> Fields:
        return self._fields

    @property
    def body(self) -> AsyncBytesReader:
        return self._body

    @property
    def reason(self) -> str | None:
        """Optional string provided by the server explaining the status."""
        # TODO: See how CRT exposes reason.
        return None

    async def chunks(self) -> AsyncGenerator[bytes, None]:
        """Yield the response body chunks as they arrive.

        :raises ClientTimeoutError: If a chunk takes longer than the read timeout.
        """
        while True:
            if self._connection is None:
                chunk = await self._stream.get_next_response_chunk()
            else:
                try:
                    chunk = await _within(
                        self._stream.get_next_response_chunk(),
                        self._read_timeout,
                        self._connection,
                    )
                except _CRTTimeoutError as e:
                    # Raised as urllib3's body raises it, so it's retried while the
                    # client reads an error or non-streaming response.
                    raise ClientTimeoutError(message=str(e)) from e
            if chunk:
                yield chunk
            else:
                break


ConnectionPoolKey = tuple[str, str, int | None, bool]
ConnectionPoolDict = dict[ConnectionPoolKey, "AIOHttpClientConnectionUnified"]


@dataclass(kw_only=True)
class AWSCRTHTTPClientConfig(http_interfaces.HTTPClientConfiguration):
    """AWS CRT HTTP client configuration.

    :param read_buffer_size: The buffer size in bytes to use when reading from streams.
        Defaults to 8192 (8 KB).
    :param trust_store_path: Path to a PEM CA bundle that overrides the default system
        trust store when verifying TLS peers. Enables connecting to a server whose
        certificate chains to a private CA.
    :param connect_timeout: How long, in seconds, to wait for a connection. The CRT
        can't wait indefinitely, so it must be more than 0 and at most 2147483.647
        (2**31 - 1 milliseconds). It's rounded to the nearest millisecond, and at
        least 1.
    """

    read_buffer_size: int = DEFAULT_READ_BUFFER_SIZE
    trust_store_path: str | None = None
    connect_timeout: float = 5.0

    def __post_init__(self) -> None:
        _assert_crt()
        # Typed settings still allow None, which the CRT can't honor.
        timeout = cast(object, self.connect_timeout)
        # Compared in milliseconds, which can be inf but never overflows.
        if not (
            isinstance(timeout, int | float) and 0 < timeout * 1000 <= _MAX_TIMEOUT_MS
        ):
            raise SmithyHTTPError(
                "The crt transport's connect_timeout must be a number of seconds "
                f"more than 0 and at most {_MAX_TIMEOUT_MS / 1000}, got "
                f"{self.connect_timeout!r}. The CRT can't wait indefinitely to "
                "connect, so None isn't supported."
            )


class _CRTTimeoutError(SmithyHTTPError):
    """Internal wrapper for CRT timeout errors."""


class AWSCRTHTTPClient(http_aio_interfaces.AsyncHTTPClient):
    """Asynchronous HTTP client backed by the AWS CRT."""

    _HTTP_PORT = 80
    _HTTPS_PORT = 443
    _TIMEOUT_ERROR_NAMES = frozenset(["AWS_IO_SOCKET_TIMEOUT", "AWS_IO_SOCKET_CLOSED"])

    TIMEOUT_EXCEPTIONS: tuple[type[Exception], ...] = (_CRTTimeoutError,)

    # CRT can stream request and response data concurrently when the connection
    # negotiates HTTP/2.
    SUPPORTS_DUPLEX_STREAMING = True

    def __init__(self, client_config: AWSCRTHTTPClientConfig | None = None) -> None:
        """
        :param client_config: Configuration that applies to all requests made with this
        client.
        """
        _assert_crt()
        self._init(_AWSCRTEventLoop(), client_config)

    async def __aenter__(self) -> Self:
        if self._closed:
            raise SmithyHTTPError("Cannot enter an HTTP client that has been closed.")
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()

    def __deepcopy__(self, memo: Any) -> "AWSCRTHTTPClient":
        # Config is copied per operation, so copies share the event loop's thread.
        copied = AWSCRTHTTPClient.__new__(AWSCRTHTTPClient)
        copied._init(self._eventloop, deepcopy(self._config))
        return copied

    async def send(
        self,
        request: http_aio_interfaces.HTTPRequest,
        *,
        request_config: http_aio_interfaces.HTTPRequestConfiguration | None = None,
    ) -> AsyncAWSCRTHTTPResponse:
        """Send HTTP request using awscrt client.

        :param request: The request including destination URI, fields, payload.
        :param request_config: Configuration specific to this request.
        """
        if self._closed:
            raise SmithyHTTPError(
                "Cannot send a request after the HTTP client has been closed."
            )

        try:
            crt_request = self._marshal_request(request)
            require_http2 = (
                getattr(request, "http2_required", False) or self._config.force_http_2
            )
            connection = await self._get_connection(
                request.destination, require_http2=require_http2
            )

            # request_body_generator is HTTP/2-only in CRT; HTTP/1.1 must use body_stream
            if connection.version is crt_http.HttpVersion.Http2:
                crt_stream = connection.request(
                    crt_request,
                    request_body_generator=self._create_body_generator(request.body),
                )
            else:
                if (
                    body_stream := await self._create_body_stream(request.body)
                ) is not None:
                    crt_request.body_stream = body_stream
                crt_stream = connection.request(crt_request)

            return await self._await_response(
                crt_stream,
                connection,
                http_settings.read_timeout(request_config, None),
            )
        except AwsCrtError as e:
            if e.name in self._TIMEOUT_ERROR_NAMES:
                raise _CRTTimeoutError(f"CRT {e.name}: {e.message}") from e
            raise

    async def close(self) -> None:
        """Close all pooled HTTP connections."""
        if self._closed:
            return
        self._closed = True
        connections = tuple(self._connections.values())
        self._connections.clear()
        await gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )

    def _init(
        self,
        eventloop: _AWSCRTEventLoop,
        client_config: AWSCRTHTTPClientConfig | None,
    ) -> None:
        self._config = client_config or AWSCRTHTTPClientConfig()
        self._eventloop = eventloop
        self._client_bootstrap = eventloop._bootstrap  # pyright: ignore[reportPrivateUsage]
        tls_ctx_options = crt_io.TlsContextOptions()
        if self._config.trust_store_path is not None:
            tls_ctx_options.override_default_trust_store_from_path(
                None, self._config.trust_store_path
            )
        self._tls_ctx = crt_io.ClientTlsContext(tls_ctx_options)
        self._socket_options = crt_io.SocketOptions()
        # The config checked that this is in the CRT's range.
        self._socket_options.connect_timeout_ms = max(
            1, round(self._config.connect_timeout * 1000)
        )
        self._connections: ConnectionPoolDict = {}
        self._closed = False

    async def _await_response(
        self,
        stream: "AIOHttpClientStreamUnified",
        connection: "AIOHttpClientConnectionUnified",
        read_timeout: float | None,
    ) -> AsyncAWSCRTHTTPResponse:
        # The CRT sets the status and headers together.
        headers = await _within(stream.get_response_headers(), read_timeout, connection)
        status_code = await stream.get_response_status_code()
        return AsyncAWSCRTHTTPResponse(
            status=status_code,
            fields=tuples_to_fields(headers),
            stream=stream,
            read_timeout=read_timeout,
            connection=connection,
        )

    async def _create_connection(
        self, url: core_interfaces.URI, *, require_http2: bool
    ) -> "AIOHttpClientConnectionUnified":
        """Builds and validates connection to ``url``"""
        connection = await self._build_new_connection(url, require_http2=require_http2)
        await self._validate_connection(connection, require_http2=require_http2)
        return connection

    async def _get_connection(
        self, url: core_interfaces.URI, *, require_http2: bool = False
    ) -> "AIOHttpClientConnectionUnified":
        # TODO: Use CRT connection pooling instead of this basic kind
        # Key by require_http2 so an h2-required event stream never reuses a pooled
        # HTTP/1.1 connection to the same host (and vice versa).
        connection_key = (url.scheme, url.host, url.port, require_http2)
        connection = self._connections.get(connection_key)

        if connection and connection.is_open():
            return connection

        connection = await self._create_connection(url, require_http2=require_http2)
        self._connections[connection_key] = connection
        return connection

    async def _build_new_connection(
        self, url: core_interfaces.URI, *, require_http2: bool = False
    ) -> "AIOHttpClientConnectionUnified":
        if url.scheme == "http":
            port = self._HTTP_PORT
            tls_connection_options = None
        elif url.scheme == "https":
            port = self._HTTPS_PORT
            tls_connection_options = self._tls_ctx.new_connection_options()
            tls_connection_options.set_server_name(url.host)
            # Advertise only h2 when it is required so endpoints that would otherwise
            # negotiate down to http/1.1 cannot, which would then fail validation.
            # TODO: Support broader TLS configuration.
            if require_http2:
                tls_connection_options.set_alpn_list(["h2"])
            else:
                tls_connection_options.set_alpn_list(["h2", "http/1.1"])
        else:
            raise SmithyHTTPError(
                f"AWSCRTHTTPClient does not support URL scheme {url.scheme}"
            )
        if url.port is not None:
            port = url.port

        return await AIOHttpClientConnectionUnified.new(
            bootstrap=self._client_bootstrap,
            host_name=url.host,
            port=port,
            socket_options=self._socket_options,
            tls_connection_options=tls_connection_options,
        )

    async def _validate_connection(
        self, connection: "AIOHttpClientConnectionUnified", *, require_http2: bool
    ) -> None:
        """Validates an existing connection against the request's requirement.

        Checks performed:
        * If HTTP/2 is required: Is the connection HTTP/2?
        """
        if require_http2 and connection.version is not crt_http.HttpVersion.Http2:
            await connection.close()
            negotiated = crt_http.HttpVersion(connection.version).name
            raise SmithyHTTPError(f"HTTP/2 could not be negotiated: {negotiated}")

    def _render_path(self, url: core_interfaces.URI) -> str:
        path = url.path if url.path is not None else "/"
        query = f"?{url.query}" if url.query is not None else ""
        return f"{path}{query}"

    def _marshal_request(
        self, request: http_aio_interfaces.HTTPRequest
    ) -> "crt_http.HttpRequest":
        """Create :py:class:`awscrt.http.HttpRequest` from
        :py:class:`smithy_http.aio.HTTPRequest`"""
        headers_list: list[tuple[str, str]] = []
        if "host" not in request.fields:
            request.fields.set_field(
                Field(name="host", values=[request.destination.netloc])
            )

        if "accept" not in request.fields:
            request.fields.set_field(Field(name="accept", values=["*/*"]))

        for fld in request.fields.entries.values():
            if fld.kind != "header":
                continue
            headers_list.extend((fld.name, val) for val in fld.values)

        path = self._render_path(request.destination)
        headers = crt_http.HttpHeaders(headers_list)

        crt_request = crt_http.HttpRequest(
            method=request.method,
            path=path,
            headers=headers,
        )
        return crt_request

    async def _create_body_stream(
        self, body: core_aio_interfaces.StreamingBlob
    ) -> core_interfaces.BytesReader | None:
        """Convert various body types to a bytes reader for CRT HTTP/1.1."""
        if core_interfaces.is_bytes_reader(body):
            return body
        buffered = await read_streaming_blob_async(body)
        return BytesIO(buffered) if buffered else None

    async def _create_body_generator(
        self, body: core_aio_interfaces.StreamingBlob
    ) -> AsyncGenerator[bytes, None]:
        """Convert various body types to async generator for request_body_generator."""
        if isinstance(body, bytes):
            # Yield the entire body as a single chunk
            yield body
        elif isinstance(body, bytearray):
            # Convert bytearray to bytes
            yield bytes(body)
        elif isinstance(body, AsyncIterable):
            # Already async iterable, just yield from it.
            # Check this before AsyncByteStream since AsyncBytesReader implements both.
            async for chunk in body:
                if isinstance(chunk, bytearray):
                    yield bytes(chunk)
                else:
                    yield chunk
        elif iscoroutinefunction(getattr(body, "read", None)) and isinstance(
            body,  # type: ignore[reportGeneralTypeIssues]
            core_aio_interfaces.AsyncByteStream,  # type: ignore[reportGeneralTypeIssues]
        ):
            # AsyncByteStream has async read method but is not iterable
            while True:
                chunk = await body.read(self._config.read_buffer_size)
                if not chunk:
                    break
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


async def _within[T](
    awaitable: Coroutine[Any, Any, T],
    seconds: float | None,
    connection: "AIOHttpClientConnectionUnified",
) -> T:
    """Wait for a stream's response data, closing its connection if it's abandoned.

    The CRT can't cancel a single stream, and it sets the result of the future it
    holds for each wait even if that future was cancelled, which fails in its own
    thread. So a wait that times out or is cancelled closes the connection, which
    ends the stream, before the wait's future is cancelled.

    :raises _CRTTimeoutError: If the data takes longer than ``seconds``.
    """
    task = ensure_future(awaitable)
    try:
        done, _ = await wait({task}, timeout=seconds)
    except BaseException:
        await _abandon(task, connection)
        raise
    if not done:
        await _abandon(task, connection)
        raise _CRTTimeoutError(
            f"No response data arrived within the read timeout of {seconds} seconds."
        )
    return task.result()


async def _abandon(task: "Future[Any]", connection: Any) -> None:
    """Close the connection, then cancel and await the wait's task.

    Finishes even if cancelled meanwhile, and then raises the cancellation.
    """

    async def cleanup() -> None:
        with suppress(Exception):
            await connection.close()
        task.cancel()
        with suppress(Exception, CancelledError):
            await task

    cleaning = ensure_future(cleanup())
    cancelled = False
    while not cleaning.done():
        try:
            await shield(cleaning)
        except CancelledError:
            cancelled = True
    if cancelled:
        raise CancelledError


def _assert_crt() -> None:
    if not HAS_CRT:
        raise MissingDependencyError(
            "Attempted to use awscrt component, but awscrt is not installed."
        )
