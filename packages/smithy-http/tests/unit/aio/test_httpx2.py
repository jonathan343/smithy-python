#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#  pyright: reportPrivateUsage=false
import asyncio
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any, cast

import httpx2
import pytest
from smithy_core import URI
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.aio.utils import read_streaming_blob_async
from smithy_core.exceptions import ClientTimeoutError
from smithy_http import Field, Fields, HTTPRequest
from smithy_http.aio.httpx2 import (
    AsyncHTTPX2Client,
    _AsyncHTTPX2ResponseBody,
    _request_body,
)
from smithy_http.exceptions import SmithyHTTPError
from smithy_http.httpx2 import HTTPX2ClientConfig

from .._local_server import LocalHTTPServer, Reply


class _Recorder:
    """A mock httpx2 transport that records the requests it's sent."""

    def __init__(self, response: httpx2.Response | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.bodies: list[bytes] = []
        self._response = response
        self.transport = httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
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


async def _sent(request: HTTPRequest) -> tuple[httpx2.Request, bytes]:
    recorder = _Recorder()
    client = AsyncHTTPX2Client(_transport=recorder.transport)
    await client.send(request)
    return recorder.requests[0], recorder.bodies[0]


def test_does_not_support_duplex_streaming() -> None:
    assert AsyncHTTPX2Client.SUPPORTS_DUPLEX_STREAMING is False


async def test_close_is_idempotent_and_blocks_sends() -> None:
    client = AsyncHTTPX2Client()

    async with client as entered:
        assert entered is client
    await client.close()

    with pytest.raises(SmithyHTTPError, match="has been closed"):
        await client.send(_request())
    with pytest.raises(SmithyHTTPError, match="has been closed"):
        async with client:
            pass


async def test_only_the_requests_headers_are_sent() -> None:
    sent, _ = await _sent(_request(fields=[Field(name="x-amz-meta-a", values=["1"])]))
    assert sent.headers.multi_items() == [
        ("host", "example.com"),
        ("x-amz-meta-a", "1"),
        ("accept-encoding", "identity"),
    ]


async def test_trailer_fields_are_not_sent_as_headers() -> None:
    sent, _ = await _sent(
        _request(
            fields=[
                Field(name="x-amz-checksum-crc32", values=["AAAAAA=="], kind="trailer")
            ]
        )
    )
    assert "x-amz-checksum-crc32" not in sent.headers


async def test_query_is_sent_as_encoded() -> None:
    query = "filter=a%2Fb&q=hello%20world&plus=a%2Bb"
    sent, _ = await _sent(_request(path="/p", query=query))
    assert sent.extensions["target"] == f"/p?{query}".encode()


async def test_empty_unframed_body_is_omitted() -> None:
    sent, body = await _sent(_request(body=AsyncBytesReader(b"")))
    assert body == b""
    assert "content-length" not in sent.headers
    assert "transfer-encoding" not in sent.headers


async def test_unframed_readers_are_read_and_sent_with_their_length() -> None:
    sent, body = await _sent(_request(method="PUT", body=AsyncBytesReader(b"hello")))
    assert body == b"hello"
    assert sent.headers["content-length"] == "5"


async def test_framed_readers_are_sent_in_large_chunks() -> None:
    # httpx2's mock transport reads the body before handing it over, so the chunks
    # are checked where the client makes them.
    size = 300 * 1024
    request = _request(
        method="PUT",
        body=BytesIO(b"x" * size),
        fields=[Field(name="content-length", values=[str(size)])],
    )

    chunks = [chunk async for chunk in cast(Any, await _request_body(request))]

    assert [len(chunk) for chunk in chunks] == [128 * 1024, 128 * 1024, 44 * 1024]


async def test_async_iterables_are_passed_through() -> None:
    # Re-chunking would hold back each item until a full chunk had arrived.
    async def chunks() -> AsyncIterator[bytes]:
        yield b"a" * 10
        yield b"b" * 10

    body = chunks()
    request = _request(
        method="PUT",
        body=body,
        fields=[Field(name="content-length", values=["20"])],
    )

    assert await _request_body(request) is body


async def test_responses_are_not_decompressed() -> None:
    compressed = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
    response = httpx2.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=httpx2.ByteStream(compressed),
    )
    client = AsyncHTTPX2Client(_transport=_Recorder(response).transport)

    result = await client.send(_request())

    assert isinstance(result.body, _AsyncHTTPX2ResponseBody)
    assert await result.body.read() == compressed


# The tests below send real requests to a local server, so they exercise httpx2's
# connection pooling rather than a mock of it.


@pytest.mark.parametrize("path", ["/a/../b", "/a/./b", "/a//b"])
async def test_path_is_sent_unnormalized(path: str) -> None:
    with LocalHTTPServer() as server:
        async with AsyncHTTPX2Client() as client:
            response = await client.send(
                _request(path=path, query="x=1", port=server.port)
            )
            await read_streaming_blob_async(response.body)

        assert server.paths == [f"{path}?x=1"]


async def test_closing_a_read_body_keeps_its_connection() -> None:
    # The client closes the bodies of failed attempts, which it has usually read
    # already, so that close must leave the connection reusable.
    with LocalHTTPServer() as server:
        server.replies = [Reply(status=503, body=b"busy"), Reply(body=b"ok")]
        async with AsyncHTTPX2Client() as client:
            failed = await client.send(_request(port=server.port))
            assert await read_streaming_blob_async(failed.body) == b"busy"
            await cast(Any, failed.body).close()

            succeeded = await client.send(_request(port=server.port))
            assert await read_streaming_blob_async(succeeded.body) == b"ok"

        assert server.ports[0] == server.ports[1]


async def test_closing_a_partly_read_body_discards_its_connection() -> None:
    with LocalHTTPServer() as server:
        server.replies = [Reply(body=b"x" * 100_000), Reply(body=b"next")]
        async with AsyncHTTPX2Client() as client:
            partial = await client.send(_request(port=server.port))
            assert len(await cast(Any, partial.body).read(10)) == 10
            await cast(Any, partial.body).close()

            response = await client.send(_request(port=server.port))
            assert await read_streaming_blob_async(response.body) == b"next"

        assert server.ports[0] != server.ports[1]


async def test_truncated_body_raises_http_error() -> None:
    with LocalHTTPServer() as server:
        server.replies = [Reply(body=b"short", truncate=True)]
        async with AsyncHTTPX2Client() as client:
            response = await client.send(_request(port=server.port))
            with pytest.raises(SmithyHTTPError, match="Failed to read"):
                await read_streaming_blob_async(response.body)


async def test_stalled_body_raises_client_timeout() -> None:
    with LocalHTTPServer() as server:
        server.replies = [Reply(body=b"x" * 100, stall_after=10)]
        config = HTTPX2ClientConfig(read_timeout=0.2)
        async with AsyncHTTPX2Client(client_config=config) as client:
            response = await client.send(_request(port=server.port))
            with pytest.raises(ClientTimeoutError):
                await read_streaming_blob_async(response.body)


async def test_force_http_2_speaks_http_2() -> None:
    pytest.importorskip("h2")
    async with _H2Server() as port:
        config = HTTPX2ClientConfig(force_http_2=True)
        async with AsyncHTTPX2Client(client_config=config) as client:
            response = await client.send(_request(path="/a/../b", query="x", port=port))
            body = await read_streaming_blob_async(response.body)

    assert response.status == 200
    assert body == b"HTTP/2 /a/../b?x"


class _H2Server:
    """A cleartext HTTP/2 server that answers each request with its target."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        import h2.config
        import h2.connection
        import h2.events

        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        paths: dict[int, bytes] = {}
        while data := await reader.read(65536):
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    paths[event.stream_id] = dict(event.headers)[b":path"]
                elif isinstance(event, h2.events.StreamEnded):
                    body = b"HTTP/2 " + paths.pop(event.stream_id)
                    conn.send_headers(
                        event.stream_id,
                        [(":status", "200"), ("content-length", str(len(body)))],
                    )
                    conn.send_data(event.stream_id, body, end_stream=True)
            writer.write(conn.data_to_send())
            await writer.drain()
        writer.close()
