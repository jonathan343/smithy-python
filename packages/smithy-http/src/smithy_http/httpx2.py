#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""A synchronous HTTP client built on httpx2.

The client drives httpx2's transport layer directly rather than ``httpx2.Client``,
which would add cookie persistence, default headers, auth, and redirect handling
that a Smithy client does not want between it and the wire.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    # pyright doesn't like optional imports.
    import httpx2

try:
    import httpx2

    HAS_HTTPX2 = True
except ImportError:
    HAS_HTTPX2 = False  # type: ignore

from smithy_core.exceptions import ClientTimeoutError, MissingDependencyError
from smithy_core.interfaces import URI, is_bytes_reader

from smithy_http import HTTPResponse
from smithy_http.interfaces import Fields, HTTPRequest
from smithy_http.interfaces import HTTPResponse as HTTPResponseInterface

from . import tuples_to_fields
from .exceptions import SmithyHTTPError
from .interfaces import HTTPClient, HTTPClientConfiguration, HTTPRequestConfiguration

__all__ = ["HAS_HTTPX2", "HTTPX2Client", "HTTPX2ClientConfig"]

_DEFAULT_CHUNK_SIZE = 64 * 1024
_UPLOAD_CHUNK_SIZE = 128 * 1024


@dataclass(kw_only=True)
class HTTPX2ClientConfig(HTTPClientConfiguration):
    """Client-level configuration for the httpx2 HTTP clients.

    :param connect_timeout: How long, in seconds, to wait for a connection, or None
        to wait indefinitely.
    :param read_timeout: How long, in seconds, to wait for data on an open
        connection, or None to wait indefinitely. It also bounds each write. A
        request's ``HTTPRequestConfiguration.read_timeout`` takes precedence.
    :param max_pool_connections: How many idle connections to keep open for reuse,
        across all hosts. Concurrent requests beyond this open connections that
        aren't kept.
    :param http2: Whether to offer HTTP/2 during TLS negotiation. This requires the
        ``h2`` package. ``force_http_2`` disables HTTP/1.1 entirely.
    """

    connect_timeout: float | None = 60.0
    read_timeout: float | None = 60.0
    max_pool_connections: int = 10
    http2: bool = False

    def __post_init__(self) -> None:
        _assert_httpx2()


class HTTPX2Client(HTTPClient):
    """Implementation of :py:class:`.interfaces.HTTPClient` using httpx2."""

    TIMEOUT_EXCEPTIONS: tuple[type[Exception], ...] = (
        (httpx2.TimeoutException,) if HAS_HTTPX2 else ()
    )

    def __init__(
        self,
        *,
        client_config: HTTPX2ClientConfig | None = None,
        _transport: "httpx2.BaseTransport | None" = None,
    ) -> None:
        """
        :param client_config: Configuration that applies to all requests made with this
        client.
        """
        _assert_httpx2()
        self._config = client_config or HTTPX2ClientConfig()
        self._closed = False
        self._transport = _transport or httpx2.HTTPTransport(
            **_transport_kwargs(self._config)
        )

    def __enter__(self) -> Self:
        if self._closed:
            raise SmithyHTTPError("Cannot enter an HTTP client that has been closed.")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def __deepcopy__(self, memo: Any) -> Self:
        return self

    def send(
        self,
        request: HTTPRequest,
        *,
        request_config: HTTPRequestConfiguration | None = None,
    ) -> HTTPResponseInterface:
        """Send HTTP request using httpx2.

        The response body is streamed: it holds a pooled connection until it's read
        to the end, which returns the connection to the pool, or closed, which
        discards it.

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
            content=_request_body(request),
            extensions=_extensions(request.destination, self._config, request_config),
        )
        raw = self._transport.handle_request(httpx_request)
        raw.request = httpx_request
        return HTTPResponse(
            status=raw.status_code,
            fields=_response_fields(raw),
            body=_HTTPX2ResponseBody(raw),
            reason=raw.reason_phrase or None,
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        if self._closed:
            return
        self._closed = True
        self._transport.close()


def _transport_kwargs(config: HTTPX2ClientConfig) -> dict[str, Any]:
    """The arguments that build an httpx2 transport for a client config."""
    return {
        "http1": not config.force_http_2,
        "http2": config.http2 or config.force_http_2,
        "limits": httpx2.Limits(
            max_connections=None,
            max_keepalive_connections=config.max_pool_connections,
        ),
        # The Smithy client owns retries, and environment variables don't
        # configure its connections.
        "retries": 0,
        "trust_env": False,
    }


def _origin(uri: URI) -> "httpx2.URL":
    """The URL httpx2 connects to and sets the Host header from.

    The path and query are left out, since httpx2 would normalize them, removing
    "." and ".." segments, after the request was signed. The request target is
    sent as-is through the ``target`` extension instead.
    """
    return httpx2.URL(scheme=uri.scheme or "https", host=uri.host, port=uri.port)


def _target(uri: URI) -> bytes:
    target = uri.path or "/"
    if uri.query:
        target += f"?{uri.query}"
    return target.encode("ascii")


def _extensions(
    uri: URI,
    config: HTTPX2ClientConfig,
    request_config: HTTPRequestConfiguration | None,
) -> dict[str, Any]:
    read_timeout = request_config.read_timeout if request_config else None
    if read_timeout is None:
        read_timeout = config.read_timeout
    return {
        "target": _target(uri),
        "timeout": {
            "connect": config.connect_timeout,
            "read": read_timeout,
            "write": read_timeout,
            "pool": config.connect_timeout,
        },
    }


def _headers(fields: Fields) -> list[tuple[str, str]]:
    """The request headers, advertising 'identity' unless an encoding was asked for.

    httpx2 can't send request trailers, and a trailer is never a header.
    """
    headers = list(
        chain.from_iterable(fld.as_tuples() for fld in fields.get_by_type("header"))
    )
    if "accept-encoding" not in fields:
        headers.append(("Accept-Encoding", "identity"))
    return headers


def _response_fields(raw: "httpx2.Response") -> Fields:
    return tuples_to_fields(
        (name.decode("latin-1"), value.decode("latin-1"))
        for name, value in raw.headers.raw
    )


def _is_framed(fields: Fields) -> bool:
    return "content-length" in fields or "transfer-encoding" in fields


def _request_body(request: HTTPRequest) -> bytes | Iterator[bytes]:
    """The body to give httpx2, which streams readers rather than reading them first.

    httpx2 sends an iterator framed by the request's Content-Length or chunked
    transfer encoding. Without either it would switch to chunked transfer
    encoding, which services such as S3 reject, so such a body is read into memory
    and sent with its length.
    """
    body = request.body
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if is_bytes_reader(body) and _is_framed(request.fields):
        return _iter_reader(body)
    return request.consume_body()


def _iter_reader(body: Any) -> Iterator[bytes]:
    while chunk := body.read(_UPLOAD_CHUNK_SIZE):
        yield chunk


class _HTTPX2ResponseBody:
    """A streamed httpx2 response body.

    The body holds its connection until it's read to the end, which returns the
    connection to the pool for reuse, or closed, which closes the connection.

    Errors raised while reading are raised as Smithy errors: timeouts as
    :py:class:`ClientTimeoutError`, so that they're retried while the client reads
    an error or non-streaming response, and other connection failures as
    :py:class:`SmithyHTTPError`.
    """

    def __init__(self, response: "httpx2.Response") -> None:
        self._response = response
        self._chunks: Iterator[bytes] | None = None
        self._buffer = b""
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        return self.iter_chunks()

    @property
    def closed(self) -> bool:
        """Whether the body was closed."""
        return self._closed

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes, or the rest of the body if it's negative.

        An empty result means the body was read to the end.

        :raises ClientTimeoutError: If the server stops sending data for longer
            than the read timeout.
        :raises SmithyHTTPError: If the connection fails, including when it closes
            before the whole ``Content-Length`` was received.
        """
        if self._closed:
            raise ValueError("I/O operation on closed file.")
        if size < 0:
            data = self._buffer + b"".join(iter(self._next_chunk, b""))
            self._buffer = b""
            return data
        while len(self._buffer) < size and (chunk := self._next_chunk()):
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def iter_chunks(self, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        """Iterate over the body in chunks of up to ``chunk_size`` bytes."""
        while data := self.read(chunk_size):
            yield data

    def readable(self) -> bool:
        """Whether the body is readable."""
        return True

    def writeable(self) -> bool:
        """Whether the body is writeable, which it never is."""
        return False

    def seekable(self) -> bool:
        """Whether the body is seekable, which it never is."""
        return False

    def close(self) -> None:
        """Close the body, releasing its connection.

        A body that was read to the end has already returned its connection to the
        pool. Otherwise the connection is closed.
        """
        if self._closed:
            return
        self._closed = True
        self._response.close()

    def _next_chunk(self) -> bytes:
        if self._chunks is None:
            # Raw bytes, since the client asks for and decodes encodings itself.
            self._chunks = self._response.iter_raw()
        try:
            return next(self._chunks, b"")
        except HTTPX2Client.TIMEOUT_EXCEPTIONS as e:
            raise ClientTimeoutError(
                message=f"A timeout error occurred ({type(e).__name__}: {e})."
            ) from e
        except httpx2.TransportError as e:
            raise SmithyHTTPError(f"Failed to read the response body: {e}") from e


def _assert_httpx2() -> None:
    if not HAS_HTTPX2:
        raise MissingDependencyError(
            "Attempted to use the httpx2 component, but httpx2 is not installed."
        )
