# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AWS REST XML protocol and error envelopes."""

from typing import Any
from xml.etree import ElementTree as ET

from smithy_core.documents import TypeRegistry
from smithy_core.exceptions import CallError, SmithyError
from smithy_core.interfaces import StreamingBlob, TypedProperties
from smithy_core.schemas import APIOperation
from smithy_core.shapes import ShapeID
from smithy_core.traits import XMLNamespaceTrait
from smithy_http.aio.interfaces import HTTPRequest, HTTPResponse
from smithy_http.aio.protocols import HttpBindingClientProtocol
from smithy_http.deserializers import HTTPResponseDeserializer
from smithy_xml.rest import RestXMLCodec, XMLDeserializer, parse_xml

from ..utils import parse_error_code, parse_retry_after
from .protocols import ProtocolSettings


class _ErrorResponseDeserializer(HTTPResponseDeserializer):
    """Use the already parsed subtree without normalizing character references."""

    def __init__(
        self, codec: RestXMLCodec, response: HTTPResponse, error: ET.Element
    ) -> None:
        super().__init__(payload_codec=codec, response=response)
        scope = codec.scope.copy()
        # AWS also sends namespace-less error envelopes for namespaced services.
        # Only discard the inherited service default, never explicit member bindings.
        if not error.tag.startswith("{"):
            scope.pop("", None)
        self._error_reader = XMLDeserializer(error, scope)

    def _create_body_deserializer(self) -> XMLDeserializer:
        return self._error_reader


class RestXmlClientProtocol(HttpBindingClientProtocol):
    """REST bindings with XML payloads; accepts both AWS error envelope variants."""

    def __init__(self, settings: ProtocolSettings) -> None:
        namespace = None
        if settings.xml_namespace is not None:
            namespace = XMLNamespaceTrait(
                {"uri": settings.xml_namespace, "prefix": settings.xml_namespace_prefix}
            )
        self._codec = RestXMLCodec(namespace)

    @property
    def id(self) -> ShapeID:
        return ShapeID("aws.protocols#restXml")

    @property
    def payload_codec(self) -> RestXMLCodec:
        return self._codec

    @property
    def content_type(self) -> str:
        return "application/xml"

    async def _create_error(
        self,
        operation: APIOperation[Any, Any],
        request: HTTPRequest,
        response: HTTPResponse,
        response_body: StreamingBlob,
        error_registry: TypeRegistry,
        context: TypedProperties,
    ) -> CallError:
        code = "Unknown"
        message: str | None = None
        error = None
        try:
            if isinstance(response_body, bytearray):
                response_body = bytes(response_body)
            root = parse_xml(response_body)
            namespace, _, name = root.tag.rpartition("}")
            prefix = namespace + "}" if namespace else ""
            if name == "Error":
                error = root
            elif name == "ErrorResponse":
                error = next((e for e in root if e.tag == prefix + "Error"), None)
            if error is not None:
                for child in error:
                    if child.tag == prefix + "Code":
                        code = (child.text or "").strip() or code
                    elif child.tag in (prefix + "Message", prefix + "message"):
                        message = child.text or ""
        except SmithyError:
            # An error status must still produce a CallError for non-XML/empty bodies.
            pass
        if (error_id := parse_error_code(code, operation.schema.id.namespace)) is None:
            error_id = ShapeID(f"{operation.schema.id.namespace}#Unknown")
        for schema in operation.error_schemas:
            if schema.id.name == error_id.name:
                error_id = schema.id
                break
        retry_after = parse_retry_after(response)
        if error is not None and error_id in error_registry:
            error_type = error_registry.get(error_id)
            if not issubclass(error_type, CallError):
                raise SmithyError("Registered XML error is not a CallError")
            # Preserve the entire subtree, not just Code/Message, and keep headers.
            result = error_type.deserialize(
                _ErrorResponseDeserializer(self.payload_codec, response, error)
            )
            if message is not None:
                result.message = message
            result.retry_after = retry_after
            return result
        return CallError(
            message=f"{code}: {message if message is not None else 'Unknown'} (HTTP {response.status})",
            fault="client" if response.status < 500 else "server",
            is_throttling_error=response.status == 429,
            is_timeout_error=response.status == 408,
            retry_after=retry_after,
        )
