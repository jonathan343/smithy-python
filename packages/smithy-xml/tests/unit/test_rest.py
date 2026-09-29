# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from io import BytesIO

import pytest
from smithy_core.documents import Document
from smithy_core.exceptions import SmithyError
from smithy_core.prelude import DOCUMENT, STRING
from smithy_core.schemas import Schema
from smithy_core.shapes import ShapeID
from smithy_core.traits import (
    DynamicTrait,
    Trait,
    XMLAttributeTrait,
    XMLNamespaceTrait,
    XMLNameTrait,
)
from smithy_xml.rest import RestXMLCodec, parse_xml


@pytest.mark.parametrize("value", ["", '<&>"雪🙂', " \t\n\r "])
@pytest.mark.parametrize("attribute", [False, True])
def test_scalar_roundtrip(value: str, attribute: bool) -> None:
    schema = Schema.collection(
        id=ShapeID("test#Root"),
        members={
            "value": {
                "target": STRING,
                "traits": [XMLAttributeTrait()] if attribute else [],
            }
        },
    )
    codec = RestXMLCodec()
    sink = BytesIO()
    with codec.create_serializer(sink).begin_struct(schema) as serializer:
        serializer.write_string(schema.members["value"], value)
    found: list[str] = []
    codec.create_deserializer(sink.getvalue()).read_struct(
        schema, lambda member, reader: found.append(reader.read_string(member))
    )
    assert found == [value]


@pytest.mark.parametrize("bound", [False, True])
def test_attribute_prefix_binding(bound: bool) -> None:
    traits: list[Trait | DynamicTrait] = [XMLAttributeTrait(), XMLNameTrait("p:value")]
    if bound:
        traits.append(XMLNamespaceTrait({"uri": "urn:attribute", "prefix": "p"}))
    schema = Schema.collection(
        id=ShapeID("test#Root"),
        members={"value": {"target": STRING, "traits": traits}},
    )
    sink = BytesIO()
    codec = RestXMLCodec()
    if not bound:
        with pytest.raises(SmithyError, match="Unbound XML prefix"):
            with codec.create_serializer(sink).begin_struct(schema) as serializer:
                serializer.write_string(schema.members["value"], "value")
        assert sink.getvalue() == b""
    else:
        with codec.create_serializer(sink).begin_struct(schema) as serializer:
            serializer.write_string(schema.members["value"], "value")
        found: list[str] = []
        codec.create_deserializer(sink.getvalue()).read_struct(
            schema, lambda member, reader: found.append(reader.read_string(member))
        )
        assert found == ["value"]


@pytest.mark.parametrize("prefix", ["p", "ns0"])
def test_attribute_namespace_does_not_rebind_containing_element(prefix: str) -> None:
    schema = Schema.collection(
        id=ShapeID("test#Root"),
        traits=[
            XMLNameTrait(f"{prefix}:Root"),
            XMLNamespaceTrait({"uri": "urn:root", "prefix": prefix}),
        ],
        members={
            "value": {
                "target": STRING,
                "traits": [
                    XMLAttributeTrait(),
                    XMLNameTrait(f"{prefix}:value"),
                    XMLNamespaceTrait({"uri": "urn:attribute", "prefix": prefix}),
                ],
            }
        },
    )
    sink = BytesIO()
    codec = RestXMLCodec()
    with codec.create_serializer(sink).begin_struct(schema) as serializer:
        serializer.write_string(schema.members["value"], "value")
    root = parse_xml(sink.getvalue())
    assert root.tag == "{urn:root}Root"
    assert root.attrib == {"{urn:attribute}value": "value"}


def test_documents_unsupported() -> None:
    codec = RestXMLCodec()
    with pytest.raises(SmithyError, match="document"):
        codec.create_serializer(BytesIO()).write_document(DOCUMENT, Document({}))
    with pytest.raises(SmithyError, match="document"):
        codec.create_deserializer(b"<Root/>").read_document(DOCUMENT)


@pytest.mark.parametrize(
    "source", [b"<Root>", b"<Root/>garbage", b"<!DOCTYPE Root><Root/>"]
)
def test_reader_stream_failures(source: bytes) -> None:
    with pytest.raises(SmithyError):
        RestXMLCodec().create_deserializer(BytesIO(source))
