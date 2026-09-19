#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

from xml.etree.ElementTree import Element

from smithy_core.codecs import Codec
from smithy_core.deserializers import ShapeDeserializer
from smithy_core.interfaces import BytesReader, BytesWriter
from smithy_core.serializers import ShapeSerializer
from smithy_core.types import TimestampFormat

from ._private.deserializers import XMLShapeDeserializer as _XMLShapeDeserializer
from ._private.readers import XMLEventReader as _XMLEventReader
from ._private.readers import XMLParseError
from ._private.readers import tree_events as _tree_events
from ._private.readers import xml_events as _xml_events
from ._private.serializers import XMLShapeSerializer as _XMLShapeSerializer
from .settings import XMLSettings

__version__ = "0.1.0"
__all__ = ("XMLCodec", "XMLParseError", "XMLSettings", "parse_xml")


def parse_xml(source: bytes | BytesReader) -> Element:
    """Parse an entire XML document for protocol-level envelope inspection.

    Like :class:`XMLCodec`, this rejects DTDs and entity declarations, disables
    external references, and limits element nesting to 128 levels. The returned
    root or any of its children can be passed to ``create_deserializer``.
    """
    reader = _XMLEventReader(_xml_events(source), document=True)
    root = next(reader).elem
    for _ in reader:
        pass
    return root


class XMLCodec(Codec):
    """A codec for converting shapes to/from XML.

    Deserialization rejects DTDs and entity declarations, disables external
    references, and limits element nesting to 128 levels. Documents and flattened
    members are buffered; this codec does not provide bounded-memory streaming.
    """

    def __init__(
        self,
        use_timestamp_format: bool = True,
        default_timestamp_format: TimestampFormat = TimestampFormat.DATE_TIME,
        default_namespace: str | None = None,
        default_namespace_prefix: str | None = None,
    ) -> None:
        """Initializes an XMLCodec.

        :param use_timestamp_format: Whether the codec should use the
            `smithy.api#timestampFormat` trait, if present.
        :param default_timestamp_format: The default timestamp format to use if the
            `smithy.api#timestampFormat` trait is not enabled or not present.
        :param default_namespace: Default XML namespace (`xmlns`) applied to the root
            element during serialization when the root shape has no
            `smithy.api#xmlNamespace` trait of its own.
        :param default_namespace_prefix: Prefix for the default namespace. When set,
            the root element declares `xmlns:<prefix>` instead of the default `xmlns`.
            Has no effect unless `default_namespace` is also set.
        """
        self._settings = XMLSettings(
            use_timestamp_format=use_timestamp_format,
            default_timestamp_format=default_timestamp_format,
            default_namespace=default_namespace,
            default_namespace_prefix=default_namespace_prefix,
        )

    @property
    def media_type(self) -> str:
        return "application/xml"

    def create_serializer(self, sink: BytesWriter) -> ShapeSerializer:
        return _XMLShapeSerializer.for_sink(sink, self._settings)

    def create_deserializer(
        self, source: bytes | BytesReader | Element
    ) -> ShapeDeserializer:
        """Create a deserializer for an XML document.

        :param source: The document to read. This may be the raw document, or an
            already parsed :py:class:`Element`, in which case that element is
            treated as the root of the document. Passing an element allows a
            document to be inspected before it is deserialized without having to
            parse it twice. Elements must have been parsed safely, for example
            with :func:`parse_xml`; entity expansion cannot be undone afterwards.
        """
        if isinstance(source, Element):
            reader = _XMLEventReader(_tree_events(source), document=True)
        else:
            reader = _XMLEventReader(_xml_events(source), document=True)
        return _XMLShapeDeserializer(settings=self._settings, reader=reader)
