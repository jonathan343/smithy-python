# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit member namespace scope, fragmented transport, and parser reuse."""

from xml.etree import ElementTree as ET

import pytest
from parentprobe.client import Probe
from parentprobe.config import Config
from parentprobe.models import Inner, ProbeScopeInput, ProbeScopeOutput
from smithy_core.aio.retries import SimpleRetryStrategy
from smithy_core.aio.types import AsyncBytesReader
from smithy_core.aio.utils import async_list
from smithy_core.exceptions import SmithyError
from smithy_http.testing.mockhttp import MockHTTPClient

RESPONSE = (
    b'<a:Response xmlns:a="urn:outer" xmlns:b="urn:inner" xmlns:f="urn:foreign">'
    b'<f:after>wrong</f:after><b:nested b:tag="x&#9;y&#10;z&#13;w">'
    b"<b:value>inside&#13;end</b:value></b:nested><a:after>outside</a:after>"
    b'<b:row b:tag="one"><b:value>1</b:value></b:row>'
    b"<a:unknown><a:last>wrong</a:last></a:unknown>"
    b'<b:row b:tag="two"><b:value>2</b:value></b:row>'
    b"<a:last>last</a:last><f:after>wrong-late</f:after></a:Response>"
)


class Fragmented(MockHTTPClient):
    async def send(self, request, *, request_config=None):
        response = await super().send(request, request_config=request_config)
        response.body = async_list([RESPONSE[i : i + 1] for i in range(len(RESPONSE))])
        return response


def client_for(transport):
    return Probe(
        Config(
            endpoint_uri="https://local.invalid",
            transport=transport,
            retry_strategy=SimpleRetryStrategy(max_attempts=1),
        )
    )


@pytest.mark.parametrize("transport_type", [MockHTTPClient, Fragmented])
async def test_generated_namespace_scope(transport_type):
    transport = transport_type()
    transport.add_response(body=RESPONSE)
    value = ProbeScopeInput(
        nested=Inner(tag="x\ty\nz\rw", value="inside\rend"),
        after="outside",
        rows=[Inner(tag="one", value="1"), Inner(tag="two", value="2")],
        last="last",
    )
    output = await client_for(transport).probe_scope(value)
    assert output == ProbeScopeOutput(
        nested=value.nested,
        after=value.after,
        rows=value.rows,
        last=value.last,
    )
    body = await AsyncBytesReader(transport.captured_requests[0].body).read()
    root = ET.fromstring(body)
    assert root.tag == "{urn:outer}Request"
    assert [child.tag for child in root] == [
        "{urn:inner}nested",
        "{urn:outer}after",
        "{urn:inner}row",
        "{urn:inner}row",
        "{urn:outer}last",
    ]
    assert root[0].attrib == {"{urn:inner}tag": "x\ty\nz\rw"}
    assert root[0][0].text == "inside\rend"


async def test_codec_reuse_after_corrupt_response():
    transport = MockHTTPClient()
    transport.add_response(body=b"<bad>")
    transport.add_response(body=RESPONSE)
    client = client_for(transport)
    with pytest.raises(SmithyError):
        await client.probe_scope(ProbeScopeInput())
    output = await client.probe_scope(ProbeScopeInput())
    assert output.after == "outside"
    assert output.last == "last"
