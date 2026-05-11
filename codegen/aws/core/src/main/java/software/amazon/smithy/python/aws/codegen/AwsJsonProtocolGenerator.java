/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
package software.amazon.smithy.python.aws.codegen;

import java.util.Set;
import software.amazon.smithy.aws.traits.protocols.AwsJson1_0Trait;
import software.amazon.smithy.aws.traits.protocols.AwsJson1_1Trait;
import software.amazon.smithy.aws.traits.protocols.AwsProtocolTrait;
import software.amazon.smithy.model.node.ArrayNode;
import software.amazon.smithy.model.node.ObjectNode;
import software.amazon.smithy.model.shapes.ShapeId;
import software.amazon.smithy.python.codegen.ApplicationProtocol;
import software.amazon.smithy.python.codegen.GenerationContext;
import software.amazon.smithy.python.codegen.HttpProtocolTestGenerator;
import software.amazon.smithy.python.codegen.SymbolProperties;
import software.amazon.smithy.python.codegen.generators.ProtocolGenerator;
import software.amazon.smithy.python.codegen.writer.PythonWriter;
import software.amazon.smithy.utils.SmithyInternalApi;

@SmithyInternalApi
public final class AwsJsonProtocolGenerator implements ProtocolGenerator {
    private static final Set<String> TESTS_TO_SKIP = Set.of(
            // TODO: support request compression.
            "SDKAppliedContentEncoding_awsJson1_0",
            "SDKAppendsGzipAndIgnoresHttpProvidedEncoding_awsJson1_0",
            "SDKAppliedContentEncoding_awsJson1_1",
            "SDKAppendsGzipAndIgnoresHttpProvidedEncoding_awsJson1_1",

            // TODO: support idempotency/default value behavior.
            "AwsJson10ClientPopulatesDefaultValuesInInput",
            "AwsJson10ClientSkipsTopLevelDefaultValuesInInput",
            "AwsJson10ClientUsesExplicitlyProvidedMemberValuesOverDefaults",
            "AwsJson10ClientIgnoresNonTopLevelDefaultsOnMembersWithClientOptional",
            "AwsJson10ClientPopulatesDefaultsValuesWhenMissingInResponse",
            "AwsJson10ClientErrorCorrectsWhenServerFailsToSerializeRequiredValues",
            "AwsJson10ClientErrorCorrectsWithDefaultValuesWhenServerFailsToSerializeRequiredValues",

            // TODO: support the endpoint trait.
            "AwsJson10EndpointTrait",
            "AwsJson10EndpointTraitWithHostLabel",
            "AwsJson11EndpointTrait",
            "AwsJson11EndpointTraitWithHostLabel",

            // These tests assert nan == nan, which is never true.
            "AwsJson10SupportsNaNFloatInputs",
            "AwsJson11SupportsNaNFloatInputs");

    private final ShapeId protocol;
    private final Class<? extends AwsProtocolTrait> traitClass;
    private final String contentType;
    private final String testFile;
    private final String testNamespace;

    private AwsJsonProtocolGenerator(
            ShapeId protocol,
            Class<? extends AwsProtocolTrait> traitClass,
            String contentType,
            String testFile,
            String testNamespace
    ) {
        this.protocol = protocol;
        this.traitClass = traitClass;
        this.contentType = contentType;
        this.testFile = testFile;
        this.testNamespace = testNamespace;
    }

    public static AwsJsonProtocolGenerator awsJson1_0() {
        return new AwsJsonProtocolGenerator(
                AwsJson1_0Trait.ID,
                AwsJson1_0Trait.class,
                "application/x-amz-json-1.0",
                "./tests/test_awsjson10_protocol.py",
                "tests.test_awsjson10_protocol");
    }

    public static AwsJsonProtocolGenerator awsJson1_1() {
        return new AwsJsonProtocolGenerator(
                AwsJson1_1Trait.ID,
                AwsJson1_1Trait.class,
                "application/x-amz-json-1.1",
                "./tests/test_awsjson11_protocol.py",
                "tests.test_awsjson11_protocol");
    }

    @Override
    public ShapeId getProtocol() {
        return protocol;
    }

    @Override
    public ApplicationProtocol getApplicationProtocol(GenerationContext context) {
        var service = context.settings().service(context.model());
        var trait = service.expectTrait(traitClass);
        var config = ObjectNode.builder()
                .withMember("http", ArrayNode.fromStrings(trait.getHttp()))
                .withMember("eventStreamHttp", ArrayNode.fromStrings(trait.getEventStreamHttp()))
                .build();
        return ApplicationProtocol.createDefaultHttpApplicationProtocol(config);
    }

    @Override
    public void initializeProtocol(GenerationContext context, PythonWriter writer) {
        writer.addDependency(AwsPythonDependency.SMITHY_AWS_CORE.withOptionalDependencies("json"));
        writer.addImport("smithy_aws_core.aio.protocols", "AwsJsonClientProtocol");
        writer.addImport("smithy_core.shapes", "ShapeID");
        var service = context.settings().service(context.model());
        var serviceSymbol = context.symbolProvider().toSymbol(service);
        var serviceSchema = serviceSymbol.expectProperty(SymbolProperties.SCHEMA);
        writer.write("AwsJsonClientProtocol($T, ShapeID($S), $S)", serviceSchema, protocol.toString(), contentType);
    }

    @Override
    public void generateProtocolTests(GenerationContext context) {
        context.writerDelegator().useFileWriter(testFile, testNamespace, writer -> {
            new HttpProtocolTestGenerator(
                    context,
                    getProtocol(),
                    writer,
                    (shape, testCase) -> TESTS_TO_SKIP.contains(testCase.getId())).run();
        });
    }
}
