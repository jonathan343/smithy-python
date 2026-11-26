#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
#  pyright: reportPrivateUsage=false
import asyncio
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from smithy_core import URI
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.exceptions import ClientTimeoutError
from smithy_core.prelude import STRING
from smithy_core.schemas import APIOperation, Schema
from smithy_core.shapes import ShapeID, ShapeType
from smithy_core.traits import StreamingTrait
from smithy_http import Fields, HTTPRequest
from smithy_http._settings import (
    ExactRequestConfiguration,
    HTTPOptions,
    HTTPTransportHooks,
    parse_settings,
)
from smithy_http.exceptions import SmithyHTTPError
from smithy_http.interfaces import HTTPRequestConfiguration

from ._local_server import LocalHTTPServer, Reply


class _Created:
    """A transport the hooks create by name."""

    def __init__(self, options: HTTPOptions) -> None:
        self.options = options

    def send(self, request: Any, *, request_config: Any = None) -> None: ...


def _hooks() -> HTTPTransportHooks:
    return HTTPTransportHooks({"plain": _Created, "crt": _Created}, default="plain")


def _config(transport: Any = None, http: Any = None) -> Any:
    return SimpleNamespace(transport=transport, http=http, http_request_config=None)


def _base(hooks: HTTPTransportHooks, **settings: Any) -> Any:
    base = _config(**settings)
    base.transport = hooks.setup(base)
    return base


@dataclass
class _Unhashable:
    """A dataclass transport, which compares by value and so can't be hashed."""

    def send(self, request: Any) -> None: ...


class _NoWeakref:
    __slots__ = ()

    def send(self, request: Any) -> None: ...


@pytest.mark.parametrize("transport", [_Unhashable(), _NoWeakref()])
def test_injected_transports_need_not_be_hashable_or_weakly_referenced(
    transport: Any,
) -> None:
    hooks = _hooks()
    base = _base(hooks, transport=transport)
    assert base.transport is transport
    assert hooks.send_options(base, base) == {}
    call = _config(transport, {"read_timeout": 5})
    assert hooks.send_options(call, base)["request_config"].read_timeout == 5


@pytest.mark.parametrize(
    ("base", "call_http", "changed"),
    [
        # Settings the base config left out are still fixed at creation.
        ({}, {"connect_timeout": 1}, "connect_timeout"),
        ({"transport": _NoWeakref()}, {"connect_timeout": 1}, "connect_timeout"),
        (
            {"transport": "crt", "http": {"connect_timeout": 2}},
            {"crt": {"read_buffer_size": 1}},
            "crt",
        ),
        ({"transport": "crt"}, {"crt": {"read_buffer_size": 1}}, "crt"),
        (
            {"transport": "crt", "http": {"crt": {"read_buffer_size": 4}}},
            {"crt": {}},
            "crt",
        ),
        # A foreign namespace, even an empty one, is rejected on every call.
        ({"transport": "plain"}, {"crt": {}}, "crt"),
        ({"transport": "plain"}, {"crt": {"read_buffer_size": 1}}, "crt"),
        ({"transport": _NoWeakref()}, {"crt": {}}, "crt"),
    ],
)
def test_calls_cannot_change_connection_settings(
    base: dict[str, Any], call_http: dict[str, Any], changed: str
) -> None:
    hooks = _hooks()
    base_config = _base(hooks, **base)
    call = _config(base_config.transport, call_http)
    with pytest.raises(SmithyHTTPError, match=f"^{changed} can't be changed"):
        hooks.send_options(call, base_config)


def test_calls_may_repeat_the_connection_settings_they_inherit() -> None:
    hooks = _hooks()
    http = {"connect_timeout": 2, "crt": {"read_buffer_size": 4}}
    base = _base(hooks, transport="crt", http=http)
    call = _config(base.transport, {**http, "read_timeout": 3})
    assert hooks.send_options(call, base)["request_config"].read_timeout == 3
    # A call that replaces http without connection settings inherits them.
    assert hooks.send_options(_config(base.transport, {}), base) == {}


@pytest.mark.parametrize(
    "settings",
    [
        {"transport": "plain", "http": {"crt": {}}},
        {"transport": None, "http": {"crt": {}}},
        {"transport": _NoWeakref(), "http": {"crt": {}}},
    ],
)
def test_empty_foreign_namespaces_fail_at_setup(settings: dict[str, Any]) -> None:
    with pytest.raises(SmithyHTTPError, match="'crt' settings apply only"):
        _hooks().setup(_config(**settings))


@pytest.mark.parametrize("name", ["connect_timeout", "read_timeout"])
@pytest.mark.parametrize(
    "value",
    [10**1000, -(10**1000), -(10**5000)],
    # Ints with over 4300 digits can't be converted to strings for an ID.
    ids=["large", "negative", "too-long-to-print"],
)
def test_timeouts_too_large_for_a_float_are_rejected(name: str, value: int) -> None:
    with pytest.raises(SmithyHTTPError, match=f"^{name} must be a positive number"):
        parse_settings({name: value})


def test_crt_namespaces_may_be_empty_for_the_crt_transport() -> None:
    created = _hooks().setup(_config("crt", {"crt": {}}))
    assert isinstance(created, _Created)


def _send_urllib3() -> tuple[float | None, float | None]:
    """The connect and read timeouts urllib3 is given for a default request."""
    import urllib3
    from smithy_http.transports import HTTP_TRANSPORTS

    base = _base(HTTP_TRANSPORTS, transport="urllib3")
    client = base.transport
    try:
        with patch.object(client._pool, "connection_from_url") as pool:
            pool.return_value.request.return_value = Mock(
                status=200, headers=urllib3.HTTPHeaderDict(), reason="OK"
            )
            client.send(
                HTTPRequest(
                    destination=URI(host="example.com"), method="GET", fields=Fields()
                ),
                **HTTP_TRANSPORTS.send_options(base, base, created=True),
            )
        timeout = pool.return_value.request.call_args.kwargs["timeout"]
        return timeout.connect_timeout, timeout.read_timeout
    finally:
        client.close()


def _async_request() -> HTTPRequest:
    return HTTPRequest(
        method="GET",
        destination=URI(scheme="https", host="example.com", path="/"),
        body=AsyncBytesReader(b""),
        fields=Fields(),
    )


async def _send_aiohttp() -> tuple[float | None, float | None]:
    """The connect and read timeouts aiohttp is given for a default request."""
    from smithy_http.aio.transports import ASYNC_HTTP_TRANSPORTS

    base = _base(ASYNC_HTTP_TRANSPORTS, transport="aiohttp")
    client = base.transport
    try:
        response = MagicMock(status=200, headers={}, reason="OK")
        with patch.object(
            client._session, "request", AsyncMock(return_value=response)
        ) as request:
            await client.send(
                _async_request(),
                **ASYNC_HTTP_TRANSPORTS.send_options(base, base, created=True),
            )
        timeout = request.call_args.kwargs["timeout"]
        return timeout.sock_connect, timeout.sock_read
    finally:
        await client.close()


async def _send_crt() -> tuple[float | None, float | None]:
    """The connect and read timeouts the CRT is given for a default request."""
    from awscrt import http as crt_http  # type: ignore
    from smithy_http.aio.transports import ASYNC_HTTP_TRANSPORTS

    base = _base(ASYNC_HTTP_TRANSPORTS, transport="crt")
    client = base.transport
    waits: list[float | None] = []

    # The spy stands in for asyncio.wait, so it must accept the same timeout keyword.
    async def recording_wait(tasks: Any, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        waits.append(timeout)
        return await asyncio.wait(tasks, timeout=timeout)

    stream = Mock()
    stream.get_response_headers = AsyncMock(return_value=[])
    stream.get_response_status_code = AsyncMock(return_value=200)
    connection = Mock(version=crt_http.HttpVersion.Http1_1)
    connection.request = Mock(return_value=stream)
    try:
        with (
            patch.object(client, "_get_connection", AsyncMock(return_value=connection)),
            patch("smithy_http.aio.crt.wait", recording_wait),
        ):
            await client.send(
                _async_request(),
                **ASYNC_HTTP_TRANSPORTS.send_options(base, base, created=True),
            )
        # The CRT is given the connect timeout in its socket options, and the read
        # timeout bounds the wait for the response to start.
        connect_ms = client._socket_options.connect_timeout_ms
        return connect_ms / 1000, waits[0]
    finally:
        await client.close()


_BLOB_STREAM = Schema.collection(
    id=ShapeID("smithy.example#BlobStream"),
    shape_type=ShapeType.BLOB,
    traits=[StreamingTrait()],
)
_EVENT_STREAM = Schema.collection(
    id=ShapeID("smithy.example#EventStream"),
    shape_type=ShapeType.UNION,
    traits=[StreamingTrait()],
    members={"message": {"target": STRING}},
)
_PLAIN = Schema.collection(
    id=ShapeID("smithy.example#Plain"), members={"message": {"target": STRING}}
)


def _io(name: str, stream: Schema) -> Schema:
    return Schema.collection(
        id=ShapeID(f"smithy.example#{name}"),
        members={"message": {"target": STRING}, "stream": {"target": stream}},
    )


def _operation(
    input: Schema = _PLAIN,
    output: Schema = _PLAIN,
    traits: dict[str, Any] | None = None,
) -> APIOperation[Any, Any]:
    return APIOperation(
        static_schema=(
            ShapeType.OPERATION.value,
            "smithy.example",
            "Op",
            traits or {},
            input,
            output,
            (),
        )
    )


@pytest.mark.parametrize(
    ("operation", "read_timeout"),
    [
        (_operation(), 60.0),
        # Downloads keep their idle protection, without a total deadline.
        (_operation(output=_io("BlobOut", _BLOB_STREAM)), 60.0),
        # A deadline on reading the response would cut off an upload or an event
        # stream that is still active.
        (_operation(input=_io("BlobIn", _BLOB_STREAM)), None),
        (_operation(input=_io("EventsIn", _EVENT_STREAM)), None),
        (_operation(output=_io("EventsOut", _EVENT_STREAM)), None),
        (
            _operation(
                input=_io("DuplexIn", _EVENT_STREAM),
                output=_io("DuplexOut", _EVENT_STREAM),
            ),
            None,
        ),
    ],
    ids=[
        "plain",
        "output-blob",
        "input-blob",
        "input-events",
        "output-events",
        "duplex",
    ],
)
def test_streaming_operations_have_no_implicit_read_timeout(
    operation: APIOperation[Any, Any], read_timeout: float | None
) -> None:
    hooks = _hooks()
    base = _base(hooks)
    options = hooks.send_options(base, base, operation=operation, created=True)
    request_config = options["request_config"]
    assert isinstance(request_config, ExactRequestConfiguration)
    assert request_config.read_timeout == read_timeout


def _long_poll(millis: int, **schemas: Schema) -> APIOperation[Any, Any]:
    return _operation(
        **schemas, traits={"smithy.api#longPoll": {"timeoutMillis": millis}}
    )


@pytest.mark.parametrize(
    ("operation", "http", "read_timeout"),
    [
        # The service may hold the request for as long as the model says.
        (_long_poll(120_000), None, 120.0),
        (_long_poll(20_000), None, 60.0),
        (_long_poll(120_000, input=_io("PollIn", _BLOB_STREAM)), None, None),
        # A timeout the caller sets wins, even shorter than the model's.
        (_long_poll(120_000), {"read_timeout": 5}, 5),
        (_long_poll(120_000), {"read_timeout": None}, None),
    ],
    ids=["longer", "shorter", "streaming", "explicit", "explicit-none"],
)
def test_long_polls_wait_at_least_their_modeled_timeout(
    operation: APIOperation[Any, Any],
    http: dict[str, Any] | None,
    read_timeout: float | None,
) -> None:
    hooks = _hooks()
    base = _base(hooks, http=http)
    options = hooks.send_options(base, base, operation=operation, created=True)
    assert options["request_config"].read_timeout == read_timeout


@pytest.mark.parametrize(
    ("legacy", "operation", "created", "read_timeout"),
    [
        (HTTPRequestConfiguration(read_timeout=7.5), _long_poll(120_000), True, 7.5),
        (
            HTTPRequestConfiguration(read_timeout=7.5),
            _operation(input=_io("UploadIn", _BLOB_STREAM)),
            True,
            7.5,
        ),
        # A legacy read timeout of None leaves the timeout to the defaults.
        (HTTPRequestConfiguration(), _operation(), True, 60.0),
        (
            HTTPRequestConfiguration(),
            _operation(input=_io("UploadIn", _BLOB_STREAM)),
            True,
            None,
        ),
        # A transport the caller set applies its own defaults.
        (HTTPRequestConfiguration(), _operation(), False, None),
        # An exact read timeout of None disables the timeout.
        (ExactRequestConfiguration(read_timeout=None), _operation(), True, None),
        (
            ExactRequestConfiguration(read_timeout=None),
            _long_poll(120_000),
            True,
            None,
        ),
    ],
    ids=[
        "numeric",
        "numeric-upload",
        "none",
        "none-upload",
        "injected",
        "exact-none",
        "exact-none-long-poll",
    ],
)
def test_legacy_request_settings_win_unless_they_leave_the_timeout_out(
    legacy: HTTPRequestConfiguration,
    operation: APIOperation[Any, Any],
    created: bool,
    read_timeout: float | None,
) -> None:
    hooks = _hooks()
    base = _base(hooks)
    base.http_request_config = legacy
    options = hooks.send_options(base, base, operation=operation, created=created)
    request_config = options["request_config"]
    assert request_config.read_timeout == read_timeout
    if not created or isinstance(legacy, ExactRequestConfiguration):
        assert request_config is legacy


def _local_send(server: LocalHTTPServer, operation: APIOperation[Any, Any]) -> Any:
    """Send a request to the server with the transport the SDK creates."""
    from smithy_http.transports import HTTP_TRANSPORTS

    base = _base(HTTP_TRANSPORTS, transport="urllib3")
    request = HTTPRequest(
        destination=URI(scheme="http", host="127.0.0.1", port=server.port),
        method="GET",
        fields=Fields(),
    )
    options = HTTP_TRANSPORTS.send_options(
        base, base, operation=operation, created=True
    )
    return base.transport, base.transport.send(request, **options)


def test_default_read_timeouts_bound_ordinary_requests_but_not_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("smithy_http._settings.DEFAULT_READ_TIMEOUT", 0.2)
    upload = _operation(input=_io("UploadIn", _BLOB_STREAM))
    gate = threading.Event()
    with LocalHTTPServer() as server:
        server.replies = [
            Reply(stall_headers=True),
            Reply(body=b"x" * 100, stall_after=10),
            Reply(body=b"late", gate=gate),
        ]
        from smithy_http.urllib3 import Urllib3HTTPClient

        with pytest.raises(Urllib3HTTPClient.TIMEOUT_EXCEPTIONS):
            _local_send(server, _operation())

        transport, response = _local_send(server, _operation())
        with pytest.raises(ClientTimeoutError):
            response.body.read()
        transport.close()

        # The response to an upload may wait longer than the default.
        transport, response = _local_send(server, upload)
        timer = threading.Timer(0.5, gate.set)
        timer.start()
        try:
            assert response.body.read() == b"late"
        finally:
            timer.cancel()
            transport.close()


async def test_sdk_created_transports_share_60_second_timeout_defaults() -> None:
    # Ordinary requests on a transport the SDK creates by name bound connecting and
    # each read by the same defaults, whichever transport is selected.
    effective = {
        "urllib3": _send_urllib3(),
        "aiohttp": await _send_aiohttp(),
        "crt": await _send_crt(),
    }
    assert effective == {
        "urllib3": (60.0, 60.0),
        "aiohttp": (60.0, 60.0),
        "crt": (60.0, 60.0),
    }
