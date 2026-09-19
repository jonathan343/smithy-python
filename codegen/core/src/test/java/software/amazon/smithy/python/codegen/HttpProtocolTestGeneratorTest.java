/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
package software.amazon.smithy.python.codegen;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import software.amazon.smithy.python.codegen.writer.PythonWriter;

public class HttpProtocolTestGeneratorTest {

    @Test
    public void xmlComparatorPreservesListOrderAndIgnoresStructureOrder() throws IOException, InterruptedException {
        PythonSettings settings = mock(PythonSettings.class);
        when(settings.moduleName()).thenReturn("example");
        PythonWriter writer = new PythonWriter(settings, "example");
        HttpProtocolTestGenerator.writeXmlComparator(writer);

        String assertions = """

                # Structure members may be reordered.
                assert xml_to_comparable(b"<root><a>1</a><b>2</b></root>") == xml_to_comparable(
                    b"<root><b>2</b><a>1</a></root>"
                )

                # Reordering members of a wrapped list must not compare equal.
                assert xml_to_comparable(
                    b"<root><items><member>1</member><member>2</member></items></root>"
                ) != xml_to_comparable(
                    b"<root><items><member>2</member><member>1</member></items></root>"
                )

                # Reordering a flattened list's repeated siblings must not compare equal.
                assert xml_to_comparable(
                    b"<root><item>1</item><item>2</item><name>x</name></root>"
                ) != xml_to_comparable(
                    b"<root><name>x</name><item>2</item><item>1</item></root>"
                )
                """;
        Process process = new ProcessBuilder("python3", "-c", writer + assertions)
                .redirectErrorStream(true)
                .start();
        String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);

        assertEquals(0, process.waitFor(), output);
    }
}
