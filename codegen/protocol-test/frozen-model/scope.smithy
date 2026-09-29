$version: "2"
namespace cleanroom.restxml
use aws.protocols#restXml
@restXml
@xmlNamespace(uri: "urn:outer", prefix: "o")
service XmlScope { version: "2026-09-28", operations: [Exchange] }
@http(method: "POST", uri: "/exchange", code: 200)
operation Exchange { input: Input, output: Output, errors: [ModeledFailure] }
@input
@xmlName("Request")
structure Input {
    nested: Nested
    after: String
    @xmlFlattened
    @xmlName("item")
    items: Items
    @xmlFlattened
    @xmlName("entry")
    entries: Entries
}
@output
@xmlName("Response")
structure Output {
    nested: Nested
    after: String
    @xmlFlattened
    @xmlName("item")
    items: Items
    @xmlFlattened
    @xmlName("entry")
    entries: Entries
}
@xmlNamespace(uri: "urn:inner", prefix: "o")
structure Nested { value: String }
list Items { member: Nested }
map Entries { key: String, value: Nested }
@error("client")
@httpError(409)
structure ModeledFailure {
    message: String
    detail: Nested
    @httpHeader("x-trace")
    trace: String
}
