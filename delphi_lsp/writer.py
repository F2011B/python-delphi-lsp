from __future__ import annotations

from .consts import ATTRIBUTE_NAME_STRINGS, SYNTAX_NODE_NAMES
from .nodes import CompoundSyntaxNode, SyntaxNode, ValuedSyntaxNode


def _xml_encode(data: str) -> str:
    result = []
    for ch in data:
        if ch == '<':
            result.append('&lt;')
        elif ch == '>':
            result.append('&gt;')
        elif ch == '&':
            result.append('&amp;')
        elif ch == '"':
            result.append('&quot;')
        elif ch == "'":
            result.append('&apos;')
        else:
            result.append(ch)
    return ''.join(result)


class SyntaxTreeWriter:
    @staticmethod
    def to_xml(root: SyntaxNode, formatted: bool = False) -> str:
        builder: list[str] = []
        SyntaxTreeWriter._node_to_xml(builder, root, formatted, '')
        return '<?xml version="1.0"?>\n' + ''.join(builder)

    @staticmethod
    def _node_to_xml(builder: list[str], node: SyntaxNode, formatted: bool, indent: str) -> None:
        has_children = node.has_children
        new_indent = indent + '  '
        if formatted:
            builder.append(indent)
        builder.append('<' + SYNTAX_NODE_NAMES[node.typ].upper())

        if isinstance(node, CompoundSyntaxNode):
            builder.append(f' begin_line="{node.line}"')
            builder.append(f' begin_col="{node.col}"')
            builder.append(f' end_line="{node.end_line}"')
            builder.append(f' end_col="{node.end_col}"')
        else:
            builder.append(f' line="{node.line}"')
            builder.append(f' col="{node.col}"')

        if node.file_name:
            builder.append('  file="' + _xml_encode(node.file_name) + '"')

        if isinstance(node, ValuedSyntaxNode):
            builder.append(' value="' + _xml_encode(node.value) + '"')

        for attr_key, attr_value in node.attributes:
            builder.append(
                ' ' + ATTRIBUTE_NAME_STRINGS[attr_key] + '="' + _xml_encode(attr_value) + '"'
            )

        if has_children:
            builder.append('>')
        else:
            builder.append('/>')
        if formatted:
            builder.append('\n')
        for child in node.child_nodes:
            SyntaxTreeWriter._node_to_xml(builder, child, formatted, new_indent)
        if has_children:
            if formatted:
                builder.append(indent)
            builder.append('</' + SYNTAX_NODE_NAMES[node.typ].upper() + '>')
            if formatted:
                builder.append('\n')
