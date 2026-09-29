// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
$version: "2"

namespace parent.scope

use smithy.test#httpResponseTests

apply ProbeScope @httpResponseTests([
    {
        id: "ExplicitNamespaceAfter"
        protocol: aws.protocols#restXml
        code: 200
        params: { after: "outside" }
        bodyMediaType: "application/xml"
        body: "<a:Response xmlns:a=\"urn:outer\"><a:after>outside</a:after></a:Response>"
    }
])
