/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
package software.amazon.smithy.python.codegen;

import software.amazon.smithy.model.node.Node;
import software.amazon.smithy.model.shapes.MemberShape;
import software.amazon.smithy.model.traits.DefaultTrait;
import software.amazon.smithy.python.codegen.writer.PythonWriter;
import software.amazon.smithy.utils.SmithyInternalApi;

/** A modeled member default, shared by dataclass fields and operation parameters. */
@SmithyInternalApi
public record MemberDefault(String expression, boolean factory) {
    public static MemberDefault of(GenerationContext context, PythonWriter writer, MemberShape member) {
        var model = context.model();
        var symbols = context.symbolProvider();
        var node = member.expectTrait(DefaultTrait.class).toNode();
        var target = model.expectShape(member.getTarget());
        // A null document default is a fresh Document(None), not Python None.
        if (!target.isDocumentShape() && node.isNullNode()) {
            return new MemberDefault("None", false);
        }
        if (target.isTimestampShape()) {
            var value = CodegenUtils.parseTimestampNode(model, member, node);
            return new MemberDefault(CodegenUtils.getDatetimeConstructor(writer, value), false);
        } else if (target.isBlobShape()) {
            writer.addStdlibImport("base64", "b64decode");
            return new MemberDefault(String.format("b64decode(\"%s\")", node.expectStringNode().getValue()), false);
        } else if (target.isEnumShape() || target.isIntEnumShape()) {
            var symbol = symbols.toSymbol(target).expectProperty(SymbolProperties.ENUM_SYMBOL);
            return new MemberDefault(String.format("%s(%s)", writer.format("$T", symbol), Node.printJson(node)), false);
        } else if (target.isDocumentShape()) {
            var value = switch (node.getType()) {
                case NULL -> "None";
                case BOOLEAN -> node.expectBooleanNode().getValue() ? "True" : "False";
                case ARRAY -> "list()";
                case OBJECT -> "dict()";
                default -> Node.printJson(node);
            };
            return new MemberDefault(String.format("lambda: %s(%s)",
                    writer.format("$T", RuntimeTypes.DOCUMENT),
                    value), true);
        }
        return switch (node.getType()) {
            case BOOLEAN -> new MemberDefault(node.expectBooleanNode().getValue() ? "True" : "False", false);
            // Smithy permits only empty collection defaults.
            case ARRAY, OBJECT -> new MemberDefault(symbols.toSymbol(target).getName(), true);
            default -> new MemberDefault(Node.printJson(node), false);
        };
    }
}
