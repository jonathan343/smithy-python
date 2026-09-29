# smithy-xml

This package provides generic XML serialization and deserialization support
for Smithy clients and servers.

`smithy_xml.rest.RestXMLCodec` implements Smithy XML bindings for REST XML
payloads. The Java client generator selects it through
`smithy_aws_core.aio.restxml.RestXmlClientProtocol` for `aws.protocols#restXml`
services and includes the `smithy-aws-core[xml]` dependency.

The REST codec supports XML names, attributes, scoped namespace declarations,
wrapped and flattened collections, structures, unions, and scalar shapes.
Namespace prefixes are declarations: use `xmlName("prefix:name")` to qualify a
name explicitly. Default namespaces qualify unprefixed elements, not attributes.
Deserialization compares expanded names, independent of response prefix spelling.
Only directly applied member namespaces affect nested elements; target-shape
namespaces apply at the document root, including structure `httpPayload` roots.
The parser rejects all DTD declarations, including internal and external entities,
and reports malformed payloads as `SmithyError`. Serialization rejects characters
outside XML 1.0's character repertoire (including unpaired surrogates) as
`SmithyError`; tabs, LF, CR and supplementary Unicode are preserved.

Modeled errors retain the parsed Error subtree and HTTP headers. Envelope Code
and Message are recognized only as immediate children in the envelope namespace.
For interoperability, an entirely namespace-less Error envelope drops only the
inherited service default namespace during error-field decoding. Explicit member
namespaces still apply, foreign-namespace fields never match by local name alone,
and normal response payloads retain strict URI matching.

XML payloads are buffered: serialization builds an element tree and renders bytes,
and deserialization buffers the HTTP body and builds a tree before constructing
models. Memory therefore scales with payload size and element count, with wire,
tree and modeled values potentially alive together. This is not streaming XML;
applications should impose transport limits appropriate to their environment.

REST XML document shapes and event streams are not supported. Host-prefix
endpoints, request compression, and automatic idempotency tokens retain the
shared runtime's existing limitations. S3-specific endpoint/addressing behavior
is not provided by this protocol implementation.

The existing `XMLCodec` and its AWS Query envelope reader remain unchanged.
