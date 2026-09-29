# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Focused wire regressions exercised through Java-generated clients."""

from xml.etree import ElementTree as ET

import pytest
from smithy_core.aio.retries import SimpleRetryStrategy
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.exceptions import SmithyError
from smithy_http.testing.mockhttp import MockHTTPClient
from xmlregressions.client import Regressions
from xmlregressions.config import Config
from xmlregressions.models import (
    BinaryInput,
    ChoiceText,
    ExchangeInput,
    Failure,
    PayloadInput,
    Root,
    TextInput,
)


def client_for(body: bytes, status: int = 200):
    transport = MockHTTPClient()
    transport.add_response(status=status, headers=[("x-trace", "trace")], body=body)
    client = Regressions(
        Config(
            endpoint_uri="https://local.invalid",
            transport=transport,
            retry_strategy=SimpleRetryStrategy(max_attempts=1),
        )
    )
    return client, transport


async def test_target_namespace_only_at_root():
    client, transport = client_for(
        b'<Data xmlns="urn:service" xmlns:f="urn:target">'
        b"<nested><value>inner</value></nested><f:nested><f:value>wrong</f:value></f:nested>"
        b"<items><item><value>one</value></item></items></Data>"
    )
    output = await client.exchange(
        ExchangeInput(nested=Root(value="inner"), items=[Root(value="one")])
    )
    assert output.nested == Root(value="inner")
    assert output.items == [Root(value="one")]
    root = ET.fromstring(
        await AsyncBytesReader(transport.captured_requests[0].body).read()
    )
    assert root[0].tag == "{urn:service}nested"
    assert root[0][0].tag == "{urn:service}value"
    assert root[1][0].tag == "{urn:service}item"
    client, transport = client_for(
        b'<Root xmlns="urn:target"><value>root</value></Root>'
    )
    assert (await client.payload(PayloadInput(value=Root(value="root")))).value == Root(
        value="root"
    )
    root = ET.fromstring(
        await AsyncBytesReader(transport.captured_requests[0].body).read()
    )
    assert root.tag == "{urn:target}Root"
    assert root[0].tag == "{urn:target}value"


async def test_qualified_map_and_unknown_wrapped_list_child():
    client, transport = client_for(
        b'<Data xmlns="urn:service" xmlns:q="urn:keys" xmlns:f="urn:foreign">'
        b"<entries><entry><q:key>a</q:key><q:value>b</q:value><f:value>wrong</f:value></entry></entries>"
        b"<items><unknown><value>wrong</value></unknown><item><value>one</value></item>"
        b"<f:item><value>wrong</value></f:item></items></Data>"
    )
    output = await client.exchange(ExchangeInput(entries={"a": "b"}))
    assert output.entries == {"a": "b"}
    assert output.items == [Root(value="one")]
    root = ET.fromstring(
        await AsyncBytesReader(transport.captured_requests[0].body).read()
    )
    assert [child.tag for child in root[0][0]] == ["{urn:keys}key", "{urn:keys}value"]


@pytest.mark.parametrize("namespace", ["", ' xmlns="urn:service"'])
@pytest.mark.parametrize("message", ["modeled&#13;message", ""])
async def test_modeled_message_without_envelope_message(namespace, message):
    client, _ = client_for(
        (
            f'<Error{namespace} xmlns:f="urn:foreign"><Code> other.namespace#Failure </Code>'
            f"<Reason>{message}</Reason><detail><value>detail&#13;end</value></detail>"
            "<f:Reason>wrong</f:Reason><f:Code>Wrong</f:Code><f:Message>wrong</f:Message></Error>"
        ).encode(),
        409,
    )
    with pytest.raises(Failure) as caught:
        await client.exchange(ExchangeInput())
    assert caught.value.message == message.replace("&#13;", "\r")
    assert caught.value.detail == Root(value="detail\rend")
    assert caught.value.trace == "trace"


async def test_normal_payload_remains_namespace_strict():
    client, _ = client_for(b"<Data><nested><value>wrong</value></nested></Data>")
    assert (await client.exchange(ExchangeInput())).nested is None


@pytest.mark.parametrize(
    "value", ["\x00", "\x01", "\x0b", "\ud800", "\udfff", "\ufffe"]
)
@pytest.mark.parametrize("attribute", [False, True])
async def test_invalid_xml_characters(value, attribute):
    client, transport = client_for(b"")
    request = (
        ExchangeInput(attribute=value)
        if attribute
        else ExchangeInput(nested=Root(value=value))
    )
    with pytest.raises(SmithyError, match="Invalid XML character"):
        await client.exchange(request)
    assert not transport.captured_requests


async def test_legal_xml_characters():
    value = "\t\n\r🙂"
    client, transport = client_for(b"")
    await client.exchange(ExchangeInput(attribute=value, nested=Root(value=value)))
    root = ET.fromstring(
        await AsyncBytesReader(transport.captured_requests[0].body).read()
    )
    assert root.attrib["attribute"] == value
    assert root[0][0].text == value


async def test_union_and_empty_missing_collections_and_204():
    client, _ = client_for(
        b'<Data xmlns="urn:service"><choice><text>hello</text></choice><items/><entries/></Data>'
    )
    result = await client.exchange(ExchangeInput(choice=ChoiceText(value="hello")))
    assert result.choice == ChoiceText(value="hello")
    assert result.items == []
    assert result.entries == {}
    for body, status in [(b'<Data xmlns="urn:service"/>', 200), (b"", 204)]:
        client, _ = client_for(body, status)
        result = await client.exchange(ExchangeInput())
        assert result.items is None
        assert result.entries is None


async def test_raw_string_and_blob_payloads():
    client, transport = client_for(b"raw<&\r")
    assert (await client.text(TextInput(value="raw<&\r"))).value == "raw<&\r"
    assert (
        await AsyncBytesReader(transport.captured_requests[0].body).read() == b"raw<&\r"
    )
    client, transport = client_for(b"\x00\xff<&")
    assert (
        await client.binary(BinaryInput(value=b"\x00\xff<&"))
    ).value == b"\x00\xff<&"
    assert (
        await AsyncBytesReader(transport.captured_requests[0].body).read()
        == b"\x00\xff<&"
    )
