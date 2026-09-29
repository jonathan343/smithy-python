// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
$version: "2"

namespace cleanroom.restxml

use smithy.test#httpRequestTests
use smithy.test#httpResponseTests

apply Exchange @httpRequestTests([
    {
        id: "FrozenScopeRequest"
        protocol: aws.protocols#restXml
        method: "POST"
        uri: "/exchange"
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
        }
        bodyMediaType: "application/xml"
        body: """
            <Request xmlns:o="urn:outer">
              <nested xmlns:o="urn:inner"><value>inner</value></nested>
              <after>outer</after>
              <item xmlns:o="urn:inner"><value>one</value></item>
              <item xmlns:o="urn:inner"><value>two</value></item>
              <entry><key>a</key><value xmlns:o="urn:inner"><value>first</value></value></entry>
              <entry><key>b</key><value xmlns:o="urn:inner"><value>second</value></value></entry>
            </Request>
            """
    }
])

apply Exchange @httpResponseTests([
    {
        id: "FrozenScopeResponseRenamedPrefixes"
        protocol: aws.protocols#restXml
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
        }
        bodyMediaType: "application/xml"
        body: """
            <Response xmlns:p="urn:outer">
              <nested xmlns:q="urn:inner"><value>inner</value></nested>
              <after>outer</after><unknown><after>ignore</after></unknown>
              <item xmlns:q="urn:inner"><value>one</value></item>
              <item xmlns:q="urn:inner"><value>two</value></item>
              <entry><key>a</key><value xmlns:q="urn:inner"><value>first</value></value></entry>
              <entry><key>b</key><value xmlns:q="urn:inner"><value>second</value></value></entry>
            </Response>
            """
    }
])

apply ModeledFailure @httpResponseTests([
    {
        id: "FrozenScopeModeledFailure"
        protocol: aws.protocols#restXml
        code: 409
        headers: { "x-trace": "trace-id" }
        params: {
            message: "bad"
            detail: { value: "inner" }
            trace: "trace-id"
        }
        bodyMediaType: "application/xml"
        body: """
            <ErrorResponse xmlns:q="urn:outer"><Error>
              <Code>ModeledFailure</Code><Message>bad</Message>
              <detail xmlns:r="urn:inner"><value>inner</value></detail>
            </Error></ErrorResponse>
            """
    }
])
