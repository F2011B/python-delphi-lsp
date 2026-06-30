from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Sequence

from .consts import AttributeName, SyntaxNodeType


class ParserException(Exception):
    def __init__(self, line: int, col: int, file_name: str, message: str) -> None:
        super().__init__(message)
        self.file_name = file_name
        self.line = line
        self.col = col


class SyntaxTreeException(ParserException):
    def __init__(self, line: int, col: int, file_name: str, message: str, syntax_tree: 'SyntaxNode') -> None:
        super().__init__(line, col, file_name, message)
        self.syntax_tree = syntax_tree


class SyntaxNode:
    def __init__(self, typ: SyntaxNodeType) -> None:
        self._typ = typ
        self._col = 0
        self._line = 0
        self._file_name = ''
        self._attributes: list[tuple[AttributeName, str]] = []
        self._child_nodes: list['SyntaxNode'] = []
        self._parent_node: Optional['SyntaxNode'] = None

    @property
    def typ(self) -> SyntaxNodeType:
        return self._typ

    @property
    def col(self) -> int:
        return self._col

    @col.setter
    def col(self, value: int) -> None:
        self._col = value

    @property
    def line(self) -> int:
        return self._line

    @line.setter
    def line(self, value: int) -> None:
        self._line = value

    @property
    def file_name(self) -> str:
        return self._file_name

    @file_name.setter
    def file_name(self, value: str) -> None:
        self._file_name = value

    @property
    def attributes(self) -> list[tuple[AttributeName, str]]:
        return self._attributes

    @property
    def child_nodes(self) -> list['SyntaxNode']:
        return self._child_nodes

    @property
    def has_attributes(self) -> bool:
        return len(self._attributes) > 0

    @property
    def has_children(self) -> bool:
        return len(self._child_nodes) > 0

    @property
    def parent_node(self) -> Optional['SyntaxNode']:
        return self._parent_node

    def assign_position_from(self, node: 'SyntaxNode') -> None:
        self._col = node.col
        self._line = node.line
        self._file_name = node.file_name

    def clone(self) -> 'SyntaxNode':
        clone_node = self.__class__(self._typ)
        clone_node._col = self._col
        clone_node._line = self._line
        clone_node._file_name = self._file_name
        clone_node._attributes = list(self._attributes)
        clone_node._child_nodes = [child.clone() for child in self._child_nodes]
        for child in clone_node._child_nodes:
            child._parent_node = clone_node
        return clone_node

    def get_attribute(self, key: AttributeName) -> str:
        for attr_key, attr_value in self._attributes:
            if attr_key == key:
                return attr_value
        return ''

    def has_attribute(self, key: AttributeName) -> bool:
        return any(attr_key == key for attr_key, _ in self._attributes)

    def set_attribute(self, key: AttributeName, value: str) -> None:
        for index, (attr_key, _) in enumerate(self._attributes):
            if attr_key == key:
                self._attributes[index] = (key, value)
                return
        self._attributes.append((key, value))

    def clear_attributes(self) -> None:
        self._attributes = []

    def add_child(self, node_or_type: 'SyntaxNode | SyntaxNodeType') -> 'SyntaxNode':
        if isinstance(node_or_type, SyntaxNodeType):
            node = SyntaxNode(node_or_type)
        else:
            node = node_or_type
        if node is None:
            raise ValueError('node must not be None')
        self._child_nodes.append(node)
        node._parent_node = self
        return node

    def extract_child(self, node: 'SyntaxNode') -> None:
        try:
            index = self._child_nodes.index(node)
        except ValueError:
            return
        self._child_nodes.pop(index)
        node._parent_node = None

    def delete_child(self, node: 'SyntaxNode') -> None:
        self.extract_child(node)

    def find_node(self, typ_or_path: SyntaxNodeType | Sequence[SyntaxNodeType]) -> Optional['SyntaxNode']:
        if isinstance(typ_or_path, SyntaxNodeType):
            for child in self._child_nodes:
                if child.typ == typ_or_path:
                    return child
            return None

        types_path = list(typ_or_path)
        if not types_path:
            return None
        if types_path[-1] == SyntaxNodeType.ntUnknown:
            return None
        return self._find_node_recursively(self, types_path, 0)

    def _find_node_recursively(
        self,
        node: 'SyntaxNode',
        types_path: Sequence[SyntaxNodeType],
        type_index: int,
    ) -> Optional['SyntaxNode']:
        for child in node.child_nodes:
            if types_path[type_index] in (child.typ, SyntaxNodeType.ntUnknown):
                if type_index < len(types_path) - 1:
                    found = self._find_node_recursively(child, types_path, type_index + 1)
                else:
                    found = child
                if found is not None:
                    return found
        return None


class CompoundSyntaxNode(SyntaxNode):
    def __init__(self, typ: SyntaxNodeType) -> None:
        super().__init__(typ)
        self.end_col = 0
        self.end_line = 0

    def clone(self) -> 'SyntaxNode':
        clone_node = super().clone()
        clone_node.end_col = self.end_col
        clone_node.end_line = self.end_line
        return clone_node


class ValuedSyntaxNode(SyntaxNode):
    def __init__(self, typ: SyntaxNodeType) -> None:
        super().__init__(typ)
        self.value = ''

    def clone(self) -> 'SyntaxNode':
        clone_node = super().clone()
        clone_node.value = self.value
        return clone_node


class CommentNode(SyntaxNode):
    def __init__(self, typ: SyntaxNodeType) -> None:
        super().__init__(typ)
        self.text = ''

    def clone(self) -> 'SyntaxNode':
        clone_node = super().clone()
        clone_node.text = self.text
        return clone_node


class OperatorKind(Enum):
    UNARY = 'unary'
    BINARY = 'binary'


class OperatorAssoc(Enum):
    LEFT = 'left'
    RIGHT = 'right'


@dataclass(frozen=True)
class OperatorInfo:
    typ: SyntaxNodeType
    priority: int
    kind: OperatorKind
    assoc: OperatorAssoc


_OPERATOR_INFO = (
    OperatorInfo(SyntaxNodeType.ntAddr, 1, OperatorKind.UNARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntDeref, 1, OperatorKind.UNARY, OperatorAssoc.LEFT),
    OperatorInfo(SyntaxNodeType.ntGeneric, 1, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntIndexed, 1, OperatorKind.UNARY, OperatorAssoc.LEFT),
    OperatorInfo(SyntaxNodeType.ntDot, 2, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntCall, 3, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntUnaryMinus, 5, OperatorKind.UNARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntNot, 6, OperatorKind.UNARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntMul, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntFDiv, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntDiv, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntMod, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntAnd, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntShl, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntShr, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntAs, 7, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntAdd, 8, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntSub, 8, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntOr, 8, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntXor, 8, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntEqual, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntNotEqual, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntLower, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntGreater, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntLowerEqual, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntGreaterEqual, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntIn, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntIs, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntNotIn, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
    OperatorInfo(SyntaxNodeType.ntIsNot, 9, OperatorKind.BINARY, OperatorAssoc.RIGHT),
)

_OPERATORS = {info.typ: info for info in _OPERATOR_INFO}


def _is_round_close(typ: SyntaxNodeType) -> bool:
    return typ == SyntaxNodeType.ntRoundClose


def _is_round_open(typ: SyntaxNodeType) -> bool:
    return typ == SyntaxNodeType.ntRoundOpen


def is_operator(typ: SyntaxNodeType) -> bool:
    return typ in _OPERATORS


def operator_info(typ: SyntaxNodeType) -> OperatorInfo:
    return _OPERATORS[typ]


def expr_to_reverse_notation(expr: Iterable[SyntaxNode]) -> list[SyntaxNode]:
    output: list[SyntaxNode] = []
    stack: list[SyntaxNode] = []
    for node in expr:
        if is_operator(node.typ):
            while stack and is_operator(stack[-1].typ):
                current = operator_info(node.typ)
                top = operator_info(stack[-1].typ)
                if (
                    (current.assoc == OperatorAssoc.LEFT and current.priority >= top.priority)
                    or (current.assoc == OperatorAssoc.RIGHT and current.priority > top.priority)
                ):
                    output.append(stack.pop())
                else:
                    break
            stack.append(node)
        elif _is_round_open(node.typ):
            stack.append(node)
        elif _is_round_close(node.typ):
            while stack and not _is_round_open(stack[-1].typ):
                output.append(stack.pop())
            if stack:
                stack.pop()
            if stack and is_operator(stack[-1].typ):
                output.append(stack.pop())
        else:
            output.append(node)
    while stack:
        output.append(stack.pop())
    return output


def node_list_to_tree(expr: Iterable[SyntaxNode], root: SyntaxNode) -> None:
    stack: list[SyntaxNode] = []
    for node in expr:
        if is_operator(node.typ):
            info = operator_info(node.typ)
            if info.kind == OperatorKind.UNARY:
                node.add_child(stack.pop())
            else:
                second = stack.pop()
                node.add_child(stack.pop())
                node.add_child(second)
        stack.append(node)
    root.add_child(stack.pop())
    if stack:
        raise ValueError('expression stack not empty after parsing')


def prepare_expr(expr_nodes: Iterable[SyntaxNode]) -> list[SyntaxNode]:
    prepared: list[SyntaxNode] = []
    prev_node: Optional[SyntaxNode] = None
    for node in expr_nodes:
        if node.typ == SyntaxNodeType.ntCall:
            continue

        if prev_node is not None and _is_round_open(node.typ):
            if not is_operator(prev_node.typ) and not _is_round_open(prev_node.typ):
                prepared.append(create_node_with_parents_position(SyntaxNodeType.ntCall, node.parent_node))
            if (
                is_operator(prev_node.typ)
                and operator_info(prev_node.typ).kind == OperatorKind.UNARY
                and operator_info(prev_node.typ).assoc == OperatorAssoc.LEFT
            ):
                prepared.append(create_node_with_parents_position(SyntaxNodeType.ntCall, node.parent_node))

        if prev_node is not None and node.typ == SyntaxNodeType.ntTypeArgs:
            if not is_operator(prev_node.typ) and prev_node.typ != SyntaxNodeType.ntTypeArgs:
                prepared.append(create_node_with_parents_position(SyntaxNodeType.ntGeneric, node.parent_node))
            if (
                is_operator(prev_node.typ)
                and operator_info(prev_node.typ).kind == OperatorKind.UNARY
                and operator_info(prev_node.typ).assoc == OperatorAssoc.LEFT
            ):
                prepared.append(create_node_with_parents_position(SyntaxNodeType.ntGeneric, node.parent_node))

        if node.typ != SyntaxNodeType.ntAlignmentParam:
            prepared.append(node.clone())
        prev_node = node

    return prepared


def create_node_with_parents_position(node_type: SyntaxNodeType, parent_node: Optional[SyntaxNode]) -> SyntaxNode:
    node = SyntaxNode(node_type)
    if parent_node is not None:
        node.assign_position_from(parent_node)
    return node


def raw_node_list_to_tree(raw_parent_node: SyntaxNode, raw_node_list: Iterable[SyntaxNode], new_root: SyntaxNode) -> None:
    try:
        prepared = prepare_expr(raw_node_list)
        reverse = expr_to_reverse_notation(prepared)
        node_list_to_tree(reverse, new_root)
    except Exception as exc:  # pragma: no cover - parity with Pascal error handling
        raise ParserException(new_root.line, new_root.col, new_root.file_name, str(exc)) from exc
