#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#  pyright: reportPrivateUsage=false
import tracemalloc
from collections.abc import Iterator
from hashlib import sha256
from io import BytesIO
from typing import Any, cast

import httpx2
import pytest
from smithy_core import URI
from smithy_core.exceptions import ClientTimeoutError
from smithy_core.interfaces import is_bytes_reader
from smithy_core.utils import read_streaming_blob
from smithy_http import Field, Fields, HTTPRequest
from smithy_http.exceptions import SmithyHTTPError
from smithy_http.httpx2 import HTTPX2Client, HTTPX2ClientConfig
from smithy_http.interfaces import HTTPRequestConfiguration

from ._local_server import LocalHTTPServer, Reply


class _Recorder:
    """A mock httpx2 transport that records the requests it's sent."""

    def __init__(self, response: httpx2.Response | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._response = response
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._response or httpx2.Response(200)


def _request(
    *,
    method: str = "GET",
    path: str | None = None,
    query: str | None = None,
    body: Any = b"",
    fields: list[Field] | None = None,
    port: int | None = None,
) -> HTTPRequest:
    return HTTPRequest(
        destination=URI(
            scheme="http" if port else "https",
            host="127.0.0.1" if port else "example.com",
            port=port,
            path=path,
            query=query,
        ),
        method=method,
        body=body,
        fields=Fields(fields or []),
    )


def _sent(
    request: HTTPRequest,
    *,
    config: HTTPX2ClientConfig | None = None,
    request_config: HTTPRequestConfiguration | None = None,
) -> httpx2.Request:
    recorder = _Recorder()
    client = HTTPX2Client(client_config=config, _transport=recorder.transport)
    client.send(request, request_config=request_config)
    return recorder.requests[0]


def test_requests_time_out_by_default() -> None:
    timeout = _sent(_request()).extensions["timeout"]
    assert timeout == {"connect": 60.0, "read": 60.0, "write": 60.0, "pool": 60.0}


def test_client_config_sets_the_timeouts() -> None:
    config = HTTPX2ClientConfig(connect_timeout=1.0, read_timeout=None)
    timeout = _sent(_request(), config=config).extensions["timeout"]
    assert timeout == {"connect": 1.0, "read": None, "write": None, "pool": 1.0}


@pytest.mark.parametrize("client_read_timeout", [None, 30.0])
def test_request_read_timeout_takes_precedence(
    client_read_timeout: float | None,
) -> None:
    timeout = _sent(
        _request(),
        config=HTTPX2ClientConfig(read_timeout=client_read_timeout),
        request_config=HTTPRequestConfiguration(read_timeout=5.0),
    ).extensions["timeout"]
    assert timeout["read"] == 5.0


def test_only_the_requests_headers_are_sent() -> None:
    # httpx2.Client would add Accept, User-Agent, Connection, and a compressed
    # Accept-Encoding, and persist cookies, so the client uses the transport alone.
    sent = _sent(_request(fields=[Field(name="x-amz-meta-a", values=["1"])]))
    assert sent.headers.multi_items() == [
        ("host", "example.com"),
        ("x-amz-meta-a", "1"),
        ("accept-encoding", "identity"),
    ]


def test_requested_accept_encoding_is_kept() -> None:
    sent = _sent(_request(fields=[Field(name="Accept-Encoding", values=["gzip"])]))
    assert sent.headers.get_list("accept-encoding") == ["gzip"]


def test_trailer_fields_are_not_sent_as_headers() -> None:
    sent = _sent(
        _request(
            method="PUT",
            fields=[
                Field(name="x-amz-meta-a", values=["1"]),
                Field(name="x-amz-checksum-crc32", values=["AAAAAA=="], kind="trailer"),
            ],
        )
    )
    assert "x-amz-checksum-crc32" not in sent.headers
    assert sent.headers["x-amz-meta-a"] == "1"


def test_content_type_is_not_added() -> None:
    sent = _sent(
        _request(
            method="PUT",
            body=b"data",
            fields=[Field(name="Content-Length", values=["4"])],
        )
    )
    assert "content-type" not in sent.headers
    assert sent.headers.get_list("content-length") == ["4"]


@pytest.mark.parametrize(
    "query",
    ["sync", "key=", "a=1&a=2", "filter=a%2Fb&q=hello%20world&plus=a%2Bb"],
)
def test_query_is_sent_as_encoded(query: str) -> None:
    sent = _sent(_request(path="/p", query=query))
    assert sent.extensions["target"] == f"/p?{query}".encode()


def test_empty_path_is_sent_as_root() -> None:
    assert _sent(_request()).extensions["target"] == b"/"


@pytest.mark.parametrize(
    ("config", "http1", "http2"),
    [
        (HTTPX2ClientConfig(), True, False),
        (HTTPX2ClientConfig(http2=True), True, True),
        (HTTPX2ClientConfig(force_http_2=True), False, True),
    ],
)
def test_http_versions_follow_the_config(
    config: HTTPX2ClientConfig, http1: bool, http2: bool
) -> None:
    pool = cast(Any, HTTPX2Client(client_config=config)._transport)._pool
    assert (pool._http1, pool._http2) == (http1, http2)


def test_pool_keeps_the_configured_connections() -> None:
    config = HTTPX2ClientConfig(max_pool_connections=25)
    pool = cast(Any, HTTPX2Client(client_config=config)._transport)._pool
    assert pool._max_keepalive_connections == 25


def test_repeated_response_headers_are_kept() -> None:
    response = httpx2.Response(200, headers=[("x-a", "1"), ("X-A", "2")])
    client = HTTPX2Client(_transport=_Recorder(response).transport)

    fields = client.send(_request()).fields

    assert fields["x-a"].values == ["1", "2"]


def test_responses_are_not_decompressed() -> None:
    compressed = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
    response = httpx2.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=httpx2.ByteStream(compressed),
    )
    client = HTTPX2Client(_transport=_Recorder(response).transport)

    assert client.send(_request()).body.read() == compressed  # type: ignore


def test_send_after_close_raises() -> None:
    client = HTTPX2Client()
    client.close()
    client.close()

    with pytest.raises(SmithyHTTPError, match="has been closed"):
        client.send(_request())
    with pytest.raises(SmithyHTTPError, match="has been closed"):
        with client:
            pass


@pytest.mark.parametrize(
    "framing",
    [
        pytest.param(Field(name="Content-Length", values=["5"]), id="content-length"),
        pytest.param(Field(name="Transfer-Encoding", values=["chunked"]), id="chunked"),
    ],
)
def test_request_readers_with_framing_are_streamed(framing: Field) -> None:
    sent = _sent(_request(method="PUT", body=BytesIO(b"hello"), fields=[framing]))

    assert isinstance(sent.stream, httpx2.SyncByteStream)
    assert sent.content == b"hello"
    assert sent.headers.get_list(framing.name) == framing.values


def test_request_readers_of_unknown_length_are_read_first() -> None:
    # httpx2 would send a reader without a length chunked, which services such as
    # S3 reject, so it's read and sent with its length.
    sent = _sent(_request(method="PUT", body=BytesIO(b"hello")))

    assert sent.headers["content-length"] == "5"
    assert "transfer-encoding" not in sent.headers


# The tests below send real requests to a local server, so they exercise httpx2's
# connection pooling rather than a mock of it.


@pytest.fixture
def server() -> Iterator[LocalHTTPServer]:
    with LocalHTTPServer() as server:
        yield server


def _local_body(client: HTTPX2Client, server: LocalHTTPServer) -> Any:
    body = client.send(_request(port=server.port)).body
    assert is_bytes_reader(body)
    return body


@pytest.mark.parametrize("path", ["/a/../b", "/a/./b", "/a//b"])
def test_path_is_sent_unnormalized(server: LocalHTTPServer, path: str) -> None:
    with HTTPX2Client() as client:
        read_streaming_blob(
            client.send(_request(path=path, query="x=1", port=server.port)).body
        )

    assert server.paths == [f"{path}?x=1"]


def test_response_body_is_streamed(server: LocalHTTPServer) -> None:
    server.replies = [Reply(body=b"0123456789")]
    body = _local_body(HTTPX2Client(), server)

    assert body.read(4) == b"0123"
    assert body.read(4) == b"4567"
    assert body.read() == b"89"
    assert body.read() == b""


def test_body_read_to_the_end_returns_connection_to_the_pool(
    server: LocalHTTPServer,
) -> None:
    server.replies = [Reply(body=b"first"), Reply(body=b"second")]
    client = HTTPX2Client()

    assert _local_body(client, server).read() == b"first"
    assert _local_body(client, server).read() == b"second"
    assert server.ports[0] == server.ports[1]


def test_closing_a_partly_read_body_discards_its_connection(
    server: LocalHTTPServer,
) -> None:
    server.replies = [Reply(body=b"x" * 100_000), Reply(body=b"next")]
    client = HTTPX2Client()

    body = _local_body(client, server)
    assert body.read(10) == b"x" * 10
    body.close()

    assert _local_body(client, server).read() == b"next"
    assert server.ports[0] != server.ports[1]


def test_read_after_close_raises(server: LocalHTTPServer) -> None:
    server.replies = [Reply(body=b"data")]
    body = _local_body(HTTPX2Client(), server)
    body.close()

    with pytest.raises(ValueError, match="closed"):
        body.read()


def test_truncated_body_raises_http_error(server: LocalHTTPServer) -> None:
    server.replies = [Reply(body=b"short", truncate=True)]
    body = _local_body(HTTPX2Client(), server)

    with pytest.raises(SmithyHTTPError, match="Failed to read the response body"):
        body.read()


def test_stalled_body_raises_client_timeout(server: LocalHTTPServer) -> None:
    server.replies = [Reply(body=b"x" * 100, stall_after=10)]
    client = HTTPX2Client(client_config=HTTPX2ClientConfig(read_timeout=0.2))
    body = _local_body(client, server)

    assert body.read(10) == b"x" * 10
    with pytest.raises(ClientTimeoutError):
        body.read()


def test_chunked_body_is_streamed(server: LocalHTTPServer) -> None:
    server.replies = [Reply(body=b"y" * 200_000, chunked=True, chunk_size=10_000)]
    body = _local_body(HTTPX2Client(), server)

    assert sum(len(chunk) for chunk in body.iter_chunks(64 * 1024)) == 200_000


def test_large_body_is_not_buffered(server: LocalHTTPServer) -> None:
    size = 32 * 1024 * 1024
    server.replies = [Reply(body=b"\0" * size)]

    tracemalloc.start()
    try:
        body = _local_body(HTTPX2Client(), server)
        received = sum(len(chunk) for chunk in body.iter_chunks(64 * 1024))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert received == size
    assert peak < size // 8


class _GeneratedReader:
    """Produces ``size`` bytes as it's read, without holding them."""

    def __init__(self, size: int) -> None:
        self._left = size
        self.digest = sha256()

    def read(self, size: int = -1) -> bytes:
        n = self._left if size < 0 else min(size, self._left)
        self._left -= n
        data = b"\0" * n
        self.digest.update(data)
        return data


def test_large_request_body_is_not_buffered(server: LocalHTTPServer) -> None:
    size = 32 * 1024 * 1024
    body = _GeneratedReader(size)
    request = _request(
        method="PUT",
        body=body,
        fields=[Field(name="Content-Length", values=[str(size)])],
        port=server.port,
    )

    tracemalloc.start()
    try:
        with HTTPX2Client() as client:
            read_streaming_blob(client.send(request).body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert server.bodies == [(size, body.digest.hexdigest())]
    assert peak < size // 8
