# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import math
from datetime import datetime
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import fromstring

import pytest
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
from smithy_xml import XMLCodec

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
