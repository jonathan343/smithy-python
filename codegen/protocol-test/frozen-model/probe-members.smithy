$version: "2"
namespace parent.scope
use aws.protocols#restXml
@restXml
@xmlNamespace(uri: "urn:outer", prefix: "p")
service Probe { version: "1", operations: [ProbeScope] }
@http(method: "POST", uri: "/probe", code: 200)
operation ProbeScope { input: ProbeScopeInput, output: ProbeScopeOutput }
@input
@xmlName("p:Request")
structure ProbeScopeInput {
    @xmlNamespace(uri: "urn:inner", prefix: "p")
    @xmlName("p:nested")
    nested: Inner
    @xmlName("p:after")
    after: String
    @xmlFlattened
    @xmlName("p:row")
    rows: Rows
    @xmlName("p:last")
    last: String
}
@output
@xmlName("p:Response")
structure ProbeScopeOutput {
    @xmlNamespace(uri: "urn:inner", prefix: "p")
    @xmlName("p:nested")
    nested: Inner
    @xmlName("p:after")
    after: String
    @xmlFlattened
    @xmlName("p:row")
    rows: Rows
    @xmlName("p:last")
    last: String
}
structure Inner {
    @xmlAttribute
    @xmlName("p:tag")
    tag: String
    @xmlName("p:value")
    value: String
}
list Rows {
    @xmlNamespace(uri: "urn:inner", prefix: "p")
    member: Inner
}
