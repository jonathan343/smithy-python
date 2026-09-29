// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
$version: "2"

namespace cleanroom.regressions

use aws.protocols#restXml

@restXml
@xmlNamespace(uri: "urn:service")
service Regressions {
    version: "1"
    operations: [
        Exchange
        Payload
        Text
        Binary
    ]
}

@smithy.test#httpRequestTests([
    {
        id: "NestedTargetNamespaceIgnored"
        protocol: restXml
        method: "POST"
        uri: "/exchange"
        params: {
            nested: { value: "inner" }
        }
        bodyMediaType: "application/xml"
        body: "<Data xmlns=\"urn:service\"><nested><value>inner</value></nested></Data>"
    }
])
@http(method: "POST", uri: "/exchange", code: 200)
operation Exchange {
    input: Data
    output: Data
    errors: [
        Failure
    ]
}

structure Data {
    nested: Root

    items: Items

    @xmlNamespace(uri: "urn:keys", prefix: "k")
    entries: Entries

    choice: Choice

    @xmlAttribute
    attribute: String
}

@xmlNamespace(uri: "urn:target")
structure Root {
    value: String
}

list Items {
    @xmlName("item")
    member: Root
}

@xmlNamespace(uri: "urn:keys", prefix: "k")
map Entries {
    @xmlName("k:key")
    key: String

    @xmlName("k:value")
    value: String
}

union Choice {
    text: String
    nested: Root
}

@error("client")
@httpError(409)
structure Failure {
    @xmlName("Reason")
    message: String

    detail: Root

    @httpHeader("x-trace")
    trace: String
}

@http(method: "POST", uri: "/payload", code: 200)
operation Payload {
    input: PayloadData
    output: PayloadData
}

structure PayloadData {
    @httpPayload
    value: Root
}

@http(method: "POST", uri: "/text", code: 200)
operation Text {
    input: TextData
    output: TextData
}

structure TextData {
    @httpPayload
    value: String
}

@http(method: "POST", uri: "/binary", code: 200)
operation Binary {
    input: BinaryData
    output: BinaryData
}

structure BinaryData {
    @httpPayload
    value: Blob
}
