# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import math
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Any
from xml.etree.ElementTree import fromstring

import pytest
from smithy_core.deserializers import ShapeDeserializer
from smithy_core.exceptions import SmithyError
from smithy_core.prelude import (
    BIG_DECIMAL,
    BLOB,
    BOOLEAN,
    DOCUMENT,
    FLOAT,
    INTEGER,
    STRING,
    TIMESTAMP,
)
from smithy_core.schemas import Schema
from smithy_core.shapes import ShapeID, ShapeType
from smithy_core.traits import XMLFlattenedTrait, XMLNamespaceTrait, XMLNameTrait
from smithy_xml import XMLCodec, XMLParseError, parse_xml

from . import (
    STRING_LIST_SCHEMA,
    STRING_MAP_SCHEMA,
    XML_SERDE_CASES,
    SerdeShape,
)


@pytest.mark.parametrize("expected, given", XML_SERDE_CASES)
def test_xml_deserializer(expected: Any, given: bytes) -> None:
    codec = XMLCodec()
    deserializer = codec.create_deserializer(given)
    match expected:
        case bool():
            actual = deserializer.read_boolean(BOOLEAN)
        case int():
            actual = deserializer.read_integer(INTEGER)
        case float():
            actual = deserializer.read_float(FLOAT)
        case Decimal():
            actual = deserializer.read_big_decimal(BIG_DECIMAL)
        case bytes():
            actual = deserializer.read_blob(BLOB)
        case str():
            actual = deserializer.read_string(STRING)
        case datetime():
            actual = deserializer.read_timestamp(TIMESTAMP)
        case list():
            actual_list: list[str] = []
            deserializer.read_list(
                STRING_LIST_SCHEMA,
                lambda d: actual_list.append(d.read_string(STRING)),
            )
            actual = actual_list
        case dict():
            actual_map: dict[str, str] = {}
            deserializer.read_map(
                STRING_MAP_SCHEMA,
                lambda k, d: actual_map.__setitem__(k, d.read_string(STRING)),
            )
            actual = actual_map
        case SerdeShape():
            actual = SerdeShape.deserialize(deserializer)
        case _:
            raise Exception(f"Unexpected type: {type(expected)}")

    assert actual == expected


def test_read_document_raises() -> None:
    """XML does not support document types."""
    deserializer = XMLCodec().create_deserializer(b"<doc>foo</doc>")
    with pytest.raises(SmithyError, match="XML does not support document types"):
        deserializer.read_document(DOCUMENT)


def test_deserialize_nan() -> None:
    actual = XMLCodec().create_deserializer(b"<f>NaN</f>").read_float(FLOAT)
    assert math.isnan(actual)


def test_deserialize_empty_string_self_closed() -> None:
    assert XMLCodec().create_deserializer(b"<s/>").read_string(STRING) == ""


def test_deserialize_empty_string_open_close() -> None:
    assert XMLCodec().create_deserializer(b"<s></s>").read_string(STRING) == ""


def test_deserialize_empty_blob() -> None:
    assert XMLCodec().create_deserializer(b"<b></b>").read_blob(BLOB) == b""


def test_deserialize_empty_blob_self_closed() -> None:
    assert XMLCodec().create_deserializer(b"<b/>").read_blob(BLOB) == b""


def test_element_source() -> None:
    """A pre-parsed element can be deserialized as the document root.

    Protocols use this to descend through transport wrappers, such as awsQuery's
    ``<OpResponse><OpResult>``, without parsing the document twice.
    """
    xml = (
        b"<OpResponse><OpResult>"
        b"<stringMember>hello</stringMember>"
        b"</OpResult></OpResponse>"
    )
    result_element = fromstring(xml)[0]
    result = SerdeShape.deserialize(XMLCodec().create_deserializer(result_element))
    assert result.string_member == "hello"


@pytest.mark.parametrize(
    "xml",
    [
        b'<Error xmlAttributeMember="modeled" />',
        b'<ErrorResponse><Error xmlAttributeMember="modeled" /></ErrorResponse>',
    ],
)
def test_element_source_attributes(xml: bytes) -> None:
    element = fromstring(xml)
    if element.tag == "ErrorResponse":
        element = element[0]
    result = SerdeShape.deserialize(XMLCodec().create_deserializer(element))
    assert result.xml_attribute_member == "modeled"


def test_element_source_scalar_read() -> None:
    element = fromstring(b"<OpResponse><OpResult>hello</OpResult></OpResponse>")[0]
    assert XMLCodec().create_deserializer(element).read_string(STRING) == "hello"


def test_flattened_list_interleaved_with_other_members() -> None:
    """Flattened list elements can be interleaved with other struct members."""
    xml = (
        b"<SerdeShape>"
        b"<flattenedListMember>first</flattenedListMember>"
        b"<stringMember>middle</stringMember>"
        b"<flattenedListMember>second</flattenedListMember>"
        b"</SerdeShape>"
    )
    result = SerdeShape.deserialize(XMLCodec().create_deserializer(xml))
    assert result.flattened_list_member == ["first", "second"]
    assert result.string_member == "middle"


def test_unknown_members_skipped() -> None:
    xml = (
        b"<SerdeShape>"
        b"<stringMember>keep</stringMember>"
        b"<unknownMember>ignore</unknownMember>"
        b"<integerMember>5</integerMember>"
        b"</SerdeShape>"
    )
    result = SerdeShape.deserialize(XMLCodec().create_deserializer(xml))
    assert result == SerdeShape(string_member="keep", integer_member=5)


@pytest.mark.parametrize("flattened", [False, True])
def test_prefixed_names_round_trip(flattened: bool) -> None:
    mapping = Schema.collection(
        id=ShapeID("test#Map"),
        shape_type=ShapeType.MAP,
        members={
            "key": {"target": STRING, "traits": [XMLNameTrait("p:key")]},
            "value": {"target": STRING, "traits": [XMLNameTrait("p:value")]},
        },
    )
    collection_traits = [XMLFlattenedTrait()] if flattened else []
    schema = Schema.collection(
        id=ShapeID("test#Root"),
        traits=[XMLNamespaceTrait({"uri": "urn:test", "prefix": "p"})],
        members={
            "text": {"target": STRING, "traits": [XMLNameTrait("p:text")]},
            "items": {
                "target": STRING_LIST_SCHEMA,
                "traits": [XMLNameTrait("p:items"), *collection_traits],
            },
            "mapping": {
                "target": mapping,
                "traits": [XMLNameTrait("p:mapping"), *collection_traits],
            },
        },
    )
    codec = XMLCodec()
    sink = BytesIO()
    with codec.create_serializer(sink).begin_struct(schema) as serializer:
        serializer.write_string(schema.members["text"], "keep")
        with serializer.begin_list(schema.members["items"], 2) as items:
            for item in ("second", "first"):
                items.write_string(STRING_LIST_SCHEMA.members["member"], item)
        with serializer.begin_map(schema.members["mapping"], 1) as entries:
            entries.entry(
                "key", lambda s: s.write_string(mapping.members["value"], "value")
            )

    result: dict[str, Any] = {}

    def consume(member: Schema, de: ShapeDeserializer) -> None:
        name = member.expect_member_name()
        if name == "text":
            result[name] = de.read_string(member)
        elif name == "items":
            result[name] = []
            de.read_list(member, lambda d: result[name].append(d.read_string(STRING)))
        else:
            result[name] = {}
            de.read_map(
                member, lambda k, d: result[name].__setitem__(k, d.read_string(STRING))
            )

    # An equivalent prefix must work as well as the serializer's chosen prefix.
    wire = sink.getvalue().replace(b"p:", b"q:").replace(b"xmlns:p", b"xmlns:q")
    codec.create_deserializer(wire).read_struct(schema, consume)
    assert result == {
        "text": "keep",
        "items": ["second", "first"],
        "mapping": {"key": "value"},
    }


def test_unknown_union_notifies_consumer_and_consumes_subtree() -> None:
    schema = Schema.collection(
        id=ShapeID("test#Union"),
        shape_type=ShapeType.UNION,
        members={"known": {"target": STRING}},
    )
    seen: list[tuple[int, str]] = []

    def consume(member: Schema, de: ShapeDeserializer) -> None:
        seen.append((member.expect_member_index(), member.expect_member_name()))
        if member.expect_member_index() == 0:
            assert de.read_string(member) == "next"

    XMLCodec().create_deserializer(
        b'<Union xmlns:p="urn:test"><p:future><nested>ignored</nested></p:future>'
        b"<known>next</known></Union>"
    ).read_struct(schema, consume)
    # The generated union consumer uses the unknown index to create its unknown
    # variant, and sees both callbacks so it can reject multiple union members.
    assert seen == [(-1, "future"), (0, "known")]


@pytest.mark.parametrize("suffix", [b"<extra/>", b"garbage", b"<!--unclosed"])
@pytest.mark.parametrize("chunked", [False, True])
def test_rejects_trailing_content(suffix: bytes, chunked: bool) -> None:
    class ChunkedReader(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(1 if size != 0 else 0)

    wire = b"<s>ok</s>" + suffix
    source = ChunkedReader(wire) if chunked else wire
    with pytest.raises(XMLParseError):
        XMLCodec().create_deserializer(source).read_string(STRING)


def test_valid_epilog_and_escaped_text() -> None:
    wire = b"<s>&lt;&amp;&#13;</s> \n<!-- comment --><?instruction value?>"
    assert XMLCodec().create_deserializer(wire).read_string(STRING) == "<&\r"
    assert parse_xml(wire).text == "<&\r"


def test_doctype_text_in_comments_and_cdata_is_not_a_declaration() -> None:
    wire = b"<!-- <!DOCTYPE s> --><s><![CDATA[<!DOCTYPE s>]]></s>"
    assert XMLCodec().create_deserializer(wire).read_string(STRING) == "<!DOCTYPE s>"


def test_text_spanning_parser_chunks() -> None:
    text = "x" * 20_000 + "\u2603<&"
    wire = b"<s>" + ("x" * 20_000 + "\u2603&lt;&amp;").encode() + b"</s>"
    assert XMLCodec().create_deserializer(wire).read_string(STRING) == text


@pytest.mark.parametrize(
    "wire",
    [
        b"<!DOCTYPE s><s>plain</s>",
        b'<!DOCTYPE s [<!ENTITY custom "expanded">]><s>&custom;</s>',
        b'<!DOCTYPE s SYSTEM "https://example.com/external.dtd"><s/>',
        b'<!DOCTYPE s [<!ENTITY custom SYSTEM "file:///not-a-real-file">]><s>&custom;</s>',
        '<!DOCTYPE s [<!ENTITY custom "expanded">]><s>&custom;</s>'.encode("utf-16"),
    ],
)
@pytest.mark.parametrize("chunked", [False, True])
def test_rejects_dtds_and_entities(wire: bytes, chunked: bool) -> None:
    class ChunkedReader(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(1 if size != 0 else 0)

    with pytest.raises(XMLParseError, match="DTD declarations are not supported"):
        XMLCodec().create_deserializer(
            ChunkedReader(wire) if chunked else wire
        ).read_string(STRING)
    with pytest.raises(XMLParseError, match="DTD declarations are not supported"):
        parse_xml(ChunkedReader(wire) if chunked else wire)


def test_rejects_doctype_before_reading_internal_subset() -> None:
    # Rejection must happen at the opening declaration, rather than after reading
    # or expanding entities in the subset. Even this incomplete DTD is rejected.
    with pytest.raises(XMLParseError, match="DTD declarations are not supported"):
        parse_xml(b"<!DOCTYPE s [")


@pytest.mark.parametrize("depth", [128, 129])
@pytest.mark.parametrize("element_source", [False, True])
def test_xml_nesting_limit_including_unknown_members(
    depth: int, element_source: bool
) -> None:
    wire = (
        b"<SerdeShape>"
        + b"<unknown>" * (depth - 1)
        + b"</unknown>" * (depth - 1)
        + b"</SerdeShape>"
    )
    source = fromstring(wire) if element_source else wire
    if depth == 128:
        assert (
            SerdeShape.deserialize(XMLCodec().create_deserializer(source))
            == SerdeShape()
        )
        assert parse_xml(wire).tag == "SerdeShape"
    else:
        with pytest.raises(XMLParseError, match="nesting depth"):
            SerdeShape.deserialize(XMLCodec().create_deserializer(source))
        with pytest.raises(XMLParseError, match="nesting depth"):
            parse_xml(wire)
