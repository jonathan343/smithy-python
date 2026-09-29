// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
$version: "2"

namespace cleanroom.namespaces

use aws.protocols#restXml
use smithy.test#httpRequestTests
use smithy.test#httpResponseTests

@restXml(noErrorWrapping: true)
@xmlNamespace(uri: "urn:outer")
service Namespaces {
    version: "1"
    operations: [
        Qualified
        DefaultScope
    ]
}

@http(method: "POST", uri: "/qualified", code: 200)
@httpRequestTests([
    {
        id: "QualifiedScopeRequest"
        protocol: restXml
        method: "POST"
        uri: "/qualified"
        params: {
            nested: { value: "inner" }
            after: "outer"
            items: [
                {
                    value: "one"
                }
                {
                    value: "two"
                }
            ]
            entries: {
                a: { value: "first" }
                b: { value: "second" }
            }
            attr: "<&\"雪"
        }
        bodyMediaType: "application/xml"
        body: """
            <o:Request xmlns="urn:outer" xmlns:o="urn:outer" attr="&lt;&amp;&quot;雪">
              <o:nested xmlns:o="urn:inner"><o:value>inner</o:value></o:nested>
              <o:after>outer</o:after>
              <o:item xmlns:o="urn:inner"><o:value>one</o:value></o:item>
              <o:item xmlns:o="urn:inner"><o:value>two</o:value></o:item>
              <o:entry><key>a</key><o:value xmlns:o="urn:inner"><o:value>first</o:value></o:value></o:entry>
              <o:entry><key>b</key><o:value xmlns:o="urn:inner"><o:value>second</o:value></o:value></o:entry>
            </o:Request>
            """
    }
])
@httpResponseTests([
    {
        id: "QualifiedScopeResponse"
        protocol: restXml
        code: 200
        params: {
            nested: { value: "inner" }
            after: "outer"
            items: [
                {
                    value: "one"
                }
                {
                    value: "two"
                }
            ]
            entries: {
                a: { value: "first" }
                b: { value: "second" }
            }
            attr: "<&\"雪"
        }
        bodyMediaType: "application/xml"
        body: """
            <p:Request xmlns="urn:outer" xmlns:p="urn:outer" xmlns:q="urn:inner" attr="&lt;&amp;&quot;雪">
              <q:nested><q:value>inner</q:value></q:nested><p:after>outer</p:after>
              <q:item><q:value>one</q:value></q:item><q:item><q:value>two</q:value></q:item>
              <p:entry><key>a</key><q:value><q:value>first</q:value></q:value></p:entry>
              <p:entry><key>b</key><q:value><q:value>second</q:value></q:value></p:entry>
            </p:Request>
            """
    }
])
operation Qualified {
    input: QualifiedData
    output: QualifiedData
    errors: [
        Failure
    ]
}

@xmlName("o:Request")
@xmlNamespace(uri: "urn:outer", prefix: "o")
structure QualifiedData {
    @xmlNamespace(uri: "urn:inner", prefix: "o")
    @xmlName("o:nested")
    nested: Nested

    @xmlName("o:after")
    after: String

    @xmlFlattened
    @xmlName("o:item")
    items: Items

    @xmlFlattened
    @xmlName("o:entry")
    entries: Entries

    @xmlAttribute
    attr: String
}

@xmlNamespace(uri: "urn:inner", prefix: "o")
structure Nested {
    @xmlName("o:value")
    value: String
}

list Items {
    @xmlNamespace(uri: "urn:inner", prefix: "o")
    member: Nested
}

map Entries {
    key: String

    @xmlNamespace(uri: "urn:inner", prefix: "o")
    @xmlName("o:value")
    value: Nested
}

@http(method: "POST", uri: "/default", code: 200)
@httpRequestTests([
    {
        id: "DefaultScopeRequest"
        protocol: restXml
        method: "POST"
        uri: "/default"
        params: {
            nested: { value: "inner" }
            after: "outer"
            items: [
                {
                    value: "one"
                }
                {
                    value: "two"
                }
            ]
        }
        bodyMediaType: "application/xml"
        body: """
            <DefaultData xmlns="urn:outer"><nested xmlns="urn:inner"><value>inner</value></nested><after>outer</after><items><member xmlns="urn:inner"><value>one</value></member><member xmlns="urn:inner"><value>two</value></member></items></DefaultData>
            """
    }
])
@httpResponseTests([
    {
        id: "DefaultScopeResponse"
        protocol: restXml
        code: 200
        params: {
            nested: { value: "inner" }
            after: "outer"
            items: [
                {
                    value: "one"
                }
                {
                    value: "two"
                }
            ]
        }
        bodyMediaType: "application/xml"
        body: """
            <p:DefaultData xmlns:p="urn:outer" xmlns:q="urn:inner"><q:nested><q:value>inner</q:value></q:nested><p:after>outer</p:after><p:items><q:member><q:value>one</q:value></q:member><q:member><q:value>two</q:value></q:member></p:items></p:DefaultData>
            """
    }
])
operation DefaultScope {
    input: DefaultData
    output: DefaultData
    errors: [
        Failure
    ]
}

structure DefaultData {
    @xmlNamespace(uri: "urn:inner")
    nested: DefaultNested

    after: String

    items: DefaultItems
}

@xmlNamespace(uri: "urn:inner")
structure DefaultNested {
    value: String
}

list DefaultItems {
    @xmlNamespace(uri: "urn:inner")
    member: DefaultNested
}

@error("client")
@httpError(409)
@httpResponseTests([
    {
        id: "NoWrappingFailure"
        protocol: restXml
        code: 409
        headers: { "x-trace": "trace" }
        params: {
            message: "bad"
            detail: { value: "inner" }
            trace: "trace"
        }
        bodyMediaType: "application/xml"
        body: """
            <p:Error xmlns:p="urn:outer" xmlns:q="urn:inner"><p:Code>Failure</p:Code><p:Message>bad</p:Message><q:detail><q:value>inner</q:value></q:detail></p:Error>
            """
    }
])
structure Failure {
    message: String

    @xmlNamespace(uri: "urn:inner")
    detail: DefaultNested

    @httpHeader("x-trace")
    trace: String
}
