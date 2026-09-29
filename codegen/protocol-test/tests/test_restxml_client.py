# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Additional end-to-end cases using Java-generated clients and local transport."""

from unittest.mock import patch

import pytest
from smithy_core.aio.retries import SimpleRetryStrategy
from smithy_core.exceptions import CallError, SmithyError
from smithy_http.testing.mockhttp import MockHTTPClient
from xmlnamespaces.client import Namespaces
from xmlnamespaces.config import Config as NamespaceConfig
from xmlnamespaces.models import DefaultNested, DefaultScopeInput, Failure
from xmlscope.client import XmlScope
from xmlscope.config import Config
from xmlscope.models import ExchangeInput, ExchangeOutput, ModeledFailure, Nested


def client_for(body: bytes, status: int = 200) -> XmlScope:
    transport = MockHTTPClient()
    transport.add_response(status=status, headers=[("x-trace", "trace")], body=body)
    return XmlScope(
        Config(
            endpoint_uri="https://local.invalid",
            transport=transport,
            retry_strategy=SimpleRetryStrategy(max_attempts=1),
        )
    )


@pytest.mark.parametrize(
    "code",
    ["ModeledFailure", "cleanroom.restxml#ModeledFailure", "ModeledFailure:extra"],
)
@pytest.mark.parametrize("wrapped", [False, True])
async def test_actual_modeled_exception_fields(code: str, wrapped: bool) -> None:
    body = f"<Error><Code>{code}</Code><Message>bad &amp; sad</Message><detail><value>inner</value></detail></Error>".encode()
    if wrapped:
        body = b"<ErrorResponse>" + body + b"<RequestId>id</RequestId></ErrorResponse>"
    with pytest.raises(ModeledFailure) as caught:
        await client_for(body, 409).exchange(ExchangeInput())
    assert type(caught.value) is ModeledFailure
    assert caught.value.message == "bad & sad"
    assert caught.value.detail == Nested(value="inner")
    assert caught.value.trace == "trace"


@pytest.mark.parametrize("message", ["a&#13;b", "", "modeled"])
async def test_error_text_and_envelope_scope(message: str) -> None:
    body = (
        '<ErrorResponse xmlns:f="urn:foreign"><f:Error><Code>Wrong</Code></f:Error>'
        "<Error><Code>  ModeledFailure  </Code>"
        f"<message>{message}</message><detail><value>a&#13;b</value>"
        "<Code>WrongNested</Code></detail><f:Code>Wrong</f:Code>"
        "<f:Message>Wrong</f:Message></Error></ErrorResponse>"
    ).encode()
    with pytest.raises(ModeledFailure) as caught:
        await client_for(body, 409).exchange(ExchangeInput())
    assert caught.value.message == message.replace("&#13;", "\r")
    assert caught.value.detail == Nested(value="a\rb")
    assert caught.value.trace == "trace"


@pytest.mark.parametrize("message", ["a&#13;b", ""])
async def test_error_capital_message(message: str) -> None:
    body = (
        f"<Error><Code>ModeledFailure</Code><Message>{message}</Message>"
        "<detail><value>a&#13;b</value></detail></Error>"
    ).encode()
    with pytest.raises(ModeledFailure) as caught:
        await client_for(body, 409).exchange(ExchangeInput())
    assert caught.value.message == message.replace("&#13;", "\r")
    assert caught.value.detail == Nested(value="a\rb")


async def test_no_error_wrapping_default_namespace_fields() -> None:
    transport = MockHTTPClient()
    transport.add_response(
        status=409,
        headers=[("x-trace", "trace")],
        body=b'<p:Error xmlns:p="urn:outer" xmlns:q="urn:inner"><p:Code>Failure</p:Code><p:Message>bad</p:Message><q:detail><q:value>inner</q:value></q:detail></p:Error>',
    )
    client = Namespaces(
        NamespaceConfig(
            endpoint_uri="https://local.invalid",
            transport=transport,
            retry_strategy=SimpleRetryStrategy(max_attempts=1),
        )
    )
    with pytest.raises(Failure) as caught:
        await client.default_scope(DefaultScopeInput())
    assert type(caught.value) is Failure
    assert caught.value.message == "bad"
    assert caught.value.detail == DefaultNested(value="inner")
    assert caught.value.trace == "trace"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not XML",
        b"<Error>",
        b"<Error><Code>Unmodeled</Code><Message>bad</Message></Error>",
    ],
)
async def test_unknown_and_corrupt_errors(body: bytes) -> None:
    with pytest.raises(CallError) as caught:
        await client_for(body, 400).exchange(ExchangeInput())
    assert type(caught.value) is CallError
    assert caught.value.fault == "client"


@pytest.mark.parametrize(
    "body",
    [
        b"<Response>",
        b"garbage",
        b"<Response/>trailing",
        b"<Response><after>&missing;</after></Response>",
    ],
)
async def test_corrupt_success_is_not_empty_output(body: bytes) -> None:
    with pytest.raises(SmithyError, match="Malformed XML"):
        await client_for(body).exchange(ExchangeInput())


async def test_empty_body_and_empty_string() -> None:
    assert await client_for(b"").exchange(ExchangeInput()) == ExchangeOutput()
    assert (
        await client_for(b"<Response><after/></Response>").exchange(ExchangeInput())
    ).after == ""


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE Response SYSTEM "file:///no-such-xml-file">',
        '<!DOCTYPE Response SYSTEM "http://local.invalid/external.dtd">',
        '<!DOCTYPE Response [<!ENTITY x SYSTEM "file:///no-such-xml-file">]>',
        '<!DOCTYPE Response [<!ENTITY x "abc"><!ENTITY y "&x;&x;&x;">]>',
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
async def test_dtd_rejected_without_file_or_network_access(
    declaration: str, encoding: str
) -> None:
    client = client_for(
        (declaration + "<Response><after>text</after></Response>").encode(encoding)
    )
    with (
        patch("builtins.open", side_effect=AssertionError("file access")) as opened,
        patch(
            "socket.socket.connect", side_effect=AssertionError("network access")
        ) as connected,
    ):
        with pytest.raises(SmithyError, match="DTD declarations are forbidden"):
            await client.exchange(ExchangeInput())
        opened.assert_not_called()
        connected.assert_not_called()
