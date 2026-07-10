from __future__ import annotations

from typing import Iterable

from .consts import SyntaxNodeType
from .nodes import CommentNode
from .preprocessor import CommentInfo


_KIND_TO_NODE_TYPE = {
    'ansi': SyntaxNodeType.ntAnsiComment,
    'borland': SyntaxNodeType.ntBorComment,
    'slashes': SyntaxNodeType.ntSlashesComment,
}


def build_comment_nodes(comments: Iterable[CommentInfo]) -> list[CommentNode]:
    nodes: list[CommentNode] = []
    for info in comments:
        node_type = _KIND_TO_NODE_TYPE.get(info.kind)
        if node_type is None:
            raise ValueError(f'unknown comment kind: {info.kind}')
        node = CommentNode(node_type)
        node.text = info.text
        node.file_name = info.file_name
        node.line = info.line
        node.col = info.col
        nodes.append(node)
    return nodes
