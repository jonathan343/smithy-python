# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""XML bindings for REST payloads, independent of the Query envelope reader."""

import re
from base64 import b64decode, b64encode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

from smithy_core.codecs import Codec
from smithy_core.deserializers import ShapeDeserializer
from smithy_core.documents import Document
from smithy_core.exceptions import SmithyError
from smithy_core.interfaces import BytesReader, BytesWriter
from smithy_core.schemas import Schema
from smithy_core.serializers import MapSerializer, ShapeSerializer
from smithy_core.shapes import ShapeID, ShapeType
from smithy_core.traits import (
    TimestampFormatTrait,
    XMLAttributeTrait,
    XMLFlattenedTrait,
    XMLNamespaceTrait,
    XMLNameTrait,
)
from smithy_core.types import TimestampFormat
from smithy_core.utils import strict_parse_bool, strict_parse_float

_INVALID_XML_CHARACTER = re.compile(
    "[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def xml_name(schema: Schema) -> str:
    trait = schema.get_trait(XMLNameTrait)
    # xmlName on a structure only names a root, not members targeting it.
    if trait is not None and (
        schema.member_target is None
        or trait is not schema.member_target.get_trait(XMLNameTrait)
    ):
        return trait.value
    if schema.member_name is None:
        original = schema.get_trait(ShapeID("smithy.synthetic#originalShapeId"))
        if original is not None and isinstance(original.document_value, str):
            return ShapeID(original.document_value).name
    return schema.member_name or schema.id.name


def scope_for(
    schema: Schema, parent: dict[str, str], *, root: bool = False
) -> dict[str, str]:
    result = parent.copy()
    trait = schema.get_trait(XMLNamespaceTrait)
    # Member schemas merge target traits. Only directly applied member namespaces
    # bind nested elements; a target namespace names the document root only.
    if trait is not None and (
        root
        or schema.member_target is None
        or trait is not schema.member_target.get_trait(XMLNamespaceTrait)
    ):
        result[trait.prefix or ""] = trait.uri
    return result


def expanded(name: str, scope: dict[str, str], attribute: bool = False) -> str:
    if ":" in name:
        prefix, name = name.split(":", 1)
        if prefix not in scope:
            raise SmithyError(f"Unbound XML prefix: {prefix}")
        uri = scope[prefix]
    else:
        uri = "" if attribute else scope.get("", "")
    return f"{{{uri}}}{name}" if uri else name


class _SafeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise SmithyError("DTD declarations are forbidden in XML payloads")


def parse_xml(source: bytes | BytesReader) -> ET.Element:
    """Parse without DTDs (including external and internal entity declarations)."""
    # The custom builder rejects every DTD before any entity can be expanded.
    parser = ET.XMLParser(target=_SafeBuilder())  # noqa: S314
    try:
        if isinstance(source, bytes):
            parser.feed(source)
        else:
            while chunk := source.read(65536):
                parser.feed(chunk)
        return parser.close()
    except ET.ParseError as exc:
        raise SmithyError(f"Malformed XML payload: {exc}") from exc


class RestXMLCodec(Codec):
    """Tree-backed XML codec implementing Smithy XML bindings.

    Namespace prefixes are declarations, not implicit additions to element names.
    Query's public XMLCodec continues to use its original envelope reader.
    """

    def __init__(self, namespace: XMLNamespaceTrait | None = None) -> None:
        self.scope = {"xml": "http://www.w3.org/XML/1998/namespace"}
        if namespace:
            self.scope[namespace.prefix or ""] = namespace.uri

    @property
    def media_type(self) -> str:
        return "application/xml"

    def create_serializer(self, sink: BytesWriter) -> ShapeSerializer:
        return XMLSerializer(sink, scope=self.scope)

    def create_deserializer(self, source: bytes | BytesReader) -> ShapeDeserializer:
        return XMLDeserializer(parse_xml(source), self.scope)


class XMLSerializer(ShapeSerializer):
    def __init__(
        self,
        sink: BytesWriter,
        parent: ET.Element | None = None,
        scope: dict[str, str] | None = None,
        item_name: str | None = None,
    ) -> None:
        self.sink = sink
        self.parent = parent
        self.scope = scope or {}
        self.item_name = item_name

    def _element(self, schema: Schema) -> tuple[ET.Element, dict[str, str]]:
        scope = scope_for(schema, self.scope, root=self.parent is None)
        name_schema = schema
        if self.parent is None and schema.member_target is not None:
            trait = schema.get_trait(XMLNameTrait)
            if trait is None or trait is schema.member_target.get_trait(XMLNameTrait):
                name_schema = schema.member_target
        name = self.item_name or xml_name(name_schema)
        expanded(name, scope)  # Reject unbound prefixes before producing invalid XML.
        element = ET.Element(name)
        for prefix, uri in scope.items():
            if prefix != "xml" and (
                self.parent is None or self.scope.get(prefix) != uri
            ):
                element.set(f"xmlns:{prefix}" if prefix else "xmlns", uri)
        if self.parent is not None:
            self.parent.append(element)
        return element, scope

    def _finish(self, element: ET.Element) -> None:
        if self.parent is None:
            # XML parsers normalize literal carriage returns, even in text nodes.
            self.sink.write(
                ET.tostring(element, encoding="utf-8").replace(b"\r", b"&#13;")
            )

    @contextmanager
    def begin_struct(self, schema: Schema) -> Iterator[ShapeSerializer]:
        element, scope = self._element(schema)
        yield XMLSerializer(self.sink, element, scope)
        self._finish(element)

    @contextmanager
    def begin_list(self, schema: Schema, size: int) -> Iterator[ShapeSerializer]:
        if XMLFlattenedTrait in schema:
            yield XMLSerializer(self.sink, self.parent, self.scope, xml_name(schema))
        else:
            element, scope = self._element(schema)
            yield XMLSerializer(self.sink, element, scope)
            self._finish(element)

    @contextmanager
    def begin_map(self, schema: Schema, size: int) -> Iterator[MapSerializer]:
        if XMLFlattenedTrait in schema:
            yield XMLMapSerializer(self, schema, xml_name(schema))
        else:
            element, scope = self._element(schema)
            yield XMLMapSerializer(
                XMLSerializer(self.sink, element, scope), schema, "entry"
            )
            self._finish(element)

    def write_string(self, schema: Schema, value: str) -> None:
        if _INVALID_XML_CHARACTER.search(value):
            raise SmithyError("Invalid XML character in serialized value")
        if XMLAttributeTrait in schema:
            if self.parent is None:
                raise SmithyError("XML attributes require a containing element")
            name = xml_name(schema)
            scope = scope_for(schema, self.scope)
            expanded(name, scope, True)  # Validate attribute prefixes too.
            if ":" in name:
                prefix, local = name.split(":", 1)
                if self.scope.get(prefix) != scope[prefix]:
                    # Do not rebind the containing element or its other attributes.
                    # Avoid ElementTree's automatic ns0 names, which can collide
                    # with the lexical namespace declarations used for elements.
                    alias = "_attribute"
                    while alias in self.scope or f"xmlns:{alias}" in self.parent.attrib:
                        alias += "_"
                    self.parent.set(f"xmlns:{alias}", scope[prefix])
                    name = f"{alias}:{local}"
            self.parent.set(name, value)
            return
        element, _ = self._element(schema)
        element.text = value
        self._finish(element)

    def write_null(self, schema: Schema) -> None:
        pass

    def write_boolean(self, schema: Schema, value: bool) -> None:
        self.write_string(schema, "true" if value else "false")

    def write_integer(self, schema: Schema, value: int) -> None:
        self.write_string(schema, str(value))

    def write_float(self, schema: Schema, value: float) -> None:
        self.write_string(
            schema,
            {"inf": "Infinity", "-inf": "-Infinity", "nan": "NaN"}.get(
                str(value), str(value)
            ),
        )

    def write_big_decimal(self, schema: Schema, value: Decimal) -> None:
        self.write_string(schema, str(value))

    def write_blob(self, schema: Schema, value: bytes) -> None:
        self.write_string(schema, b64encode(value).decode("ascii"))

    def write_timestamp(self, schema: Schema, value: datetime) -> None:
        trait = schema.get_trait(TimestampFormatTrait)
        fmt = trait.format if trait else TimestampFormat.DATE_TIME
        self.write_string(schema, str(fmt.serialize(value)))

    def write_document(self, schema: Schema, value: Document) -> None:
        raise SmithyError("REST XML does not support document shapes")


class XMLMapSerializer(MapSerializer):
    def __init__(self, serializer: XMLSerializer, schema: Schema, name: str) -> None:
        self.serializer = serializer
        self.schema = schema
        self.name = name

    def entry(self, key: str, value_writer: Callable[[ShapeSerializer], None]) -> None:
        parent = self.serializer.parent
        if parent is None:
            raise SmithyError("XML maps require a containing element")
        if XMLFlattenedTrait in self.schema:
            serializer = XMLSerializer(
                self.serializer.sink, parent, self.serializer.scope, self.name
            )
            element, scope = serializer._element(self.schema)  # pyright: ignore[reportPrivateUsage]
        else:
            element = ET.SubElement(parent, self.name)
            scope = self.serializer.scope
        serializer = XMLSerializer(self.serializer.sink, element, scope)
        serializer.write_string(self.schema.members["key"], key)
        value_writer(serializer)


class XMLDeserializer(ShapeDeserializer):
    def __init__(
        self,
        element: ET.Element,
        scope: dict[str, str],
        bound_schema: Schema | None = None,
        siblings: list[ET.Element] | None = None,
    ) -> None:
        self.element = element
        self.scope = scope
        self.bound_schema = bound_schema
        self.siblings = siblings

    def read_struct(
        self, schema: Schema, consumer: Callable[[Schema, ShapeDeserializer], None]
    ) -> None:
        scope = scope_for(self.bound_schema or schema, self.scope)
        for member in schema.members.values():
            member_scope = scope_for(member, scope)
            if XMLFlattenedTrait in member and member.shape_type is ShapeType.LIST:
                member_scope = scope_for(member.members["member"], scope)
            name = expanded(xml_name(member), member_scope, XMLAttributeTrait in member)
            if XMLAttributeTrait in member:
                if name in self.element.attrib:
                    value = ET.Element(name)
                    value.text = self.element.attrib[name]
                    consumer(member, XMLDeserializer(value, scope, member))
                continue
            elements = [e for e in self.element if e.tag == name]
            if elements:
                consumer(member, XMLDeserializer(elements[0], scope, member, elements))

    def read_list(
        self, schema: Schema, consumer: Callable[[ShapeDeserializer], None]
    ) -> None:
        bound = self.bound_schema or schema
        scope = (
            self.scope if XMLFlattenedTrait in bound else scope_for(bound, self.scope)
        )
        member = schema.members["member"]
        if XMLFlattenedTrait in bound:
            elements = self.siblings or [self.element]
        else:
            name = expanded(xml_name(member), scope_for(member, scope))
            elements = [e for e in self.element if e.tag == name]
        for element in elements:
            consumer(XMLDeserializer(element, scope, member))

    def read_map(
        self, schema: Schema, consumer: Callable[[str, ShapeDeserializer], None]
    ) -> None:
        bound = self.bound_schema or schema
        scope = scope_for(bound, self.scope)
        entries = (
            self.siblings or [self.element]
            if XMLFlattenedTrait in bound
            else [e for e in self.element if e.tag == expanded("entry", scope)]
        )
        key_schema, value_schema = schema.members["key"], schema.members["value"]
        key_name = expanded(xml_name(key_schema), scope_for(key_schema, scope))
        value_name = expanded(xml_name(value_schema), scope_for(value_schema, scope))
        for entry in entries:
            key = next((e for e in entry if e.tag == key_name), None)
            value = next((e for e in entry if e.tag == value_name), None)
            if key is None or value is None:
                raise SmithyError("XML map entry requires key and value")
            consumer(key.text or "", XMLDeserializer(value, scope, value_schema))

    def is_null(self) -> bool:
        return False

    def read_null(self) -> None:
        raise SmithyError("XML has no null value")

    def read_string(self, schema: Schema) -> str:
        return self.element.text or ""

    def read_boolean(self, schema: Schema) -> bool:
        return strict_parse_bool(self.read_string(schema))

    def read_integer(self, schema: Schema) -> int:
        return int(self.read_string(schema))

    def read_float(self, schema: Schema) -> float:
        return strict_parse_float(self.read_string(schema))

    def read_big_decimal(self, schema: Schema) -> Decimal:
        return Decimal(self.read_string(schema))

    def read_blob(self, schema: Schema) -> bytes:
        return b64decode(self.read_string(schema))

    def read_timestamp(self, schema: Schema) -> datetime:
        trait = (self.bound_schema or schema).get_trait(TimestampFormatTrait)
        fmt = trait.format if trait else TimestampFormat.DATE_TIME
        return fmt.deserialize(self.read_string(schema))

    def read_document(self, schema: Schema) -> Document:
        raise SmithyError("REST XML does not support document shapes")
