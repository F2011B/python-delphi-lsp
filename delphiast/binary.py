from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import io
import struct
from typing import BinaryIO, Optional

from .consts import AttributeName, SyntaxNodeType
from .nodes import CommentNode, CompoundSyntaxNode, SyntaxNode, ValuedSyntaxNode


class BinarySerializerError(Exception):
    pass


class NodeClass(IntEnum):
    SYNTAX = 0
    COMPOUND = 1
    VALUED = 2
    COMMENT = 3


_SIGNATURE = b'DAST binary file\x1a'
_VERSION = 0x01000000


class BinarySerializer:
    def __init__(self) -> None:
        self._string_list: list[str] = []
        self._string_table: dict[str, int] = {}

    def read(self, stream: BinaryIO) -> SyntaxNode:
        self._string_list = []
        if not self._check_signature(stream):
            raise BinarySerializerError('invalid signature')
        if not self._check_version(stream):
            raise BinarySerializerError('unsupported version')
        node = self._read_node(stream)
        if node is None:
            raise BinarySerializerError('failed to read node')
        return node

    def write(self, stream: BinaryIO, root: SyntaxNode) -> None:
        self._string_table = {}
        stream.write(_SIGNATURE)
        stream.write(struct.pack('<I', _VERSION))
        if not self._write_node(stream, root):
            raise BinarySerializerError('failed to write node')

    def _check_signature(self, stream: BinaryIO) -> bool:
        data = stream.read(len(_SIGNATURE))
        return data == _SIGNATURE

    def _check_version(self, stream: BinaryIO) -> bool:
        data = stream.read(4)
        if len(data) != 4:
            return False
        version = struct.unpack('<I', data)[0]
        return (version & 0xFFFF0000) == _VERSION

    def _create_node(self, node_class: NodeClass, node_type: SyntaxNodeType) -> SyntaxNode:
        if node_class == NodeClass.SYNTAX:
            return SyntaxNode(node_type)
        if node_class == NodeClass.COMPOUND:
            return CompoundSyntaxNode(node_type)
        if node_class == NodeClass.VALUED:
            return ValuedSyntaxNode(node_type)
        if node_class == NodeClass.COMMENT:
            return CommentNode(node_type)
        raise BinarySerializerError('unexpected node class')

    def _read_node(self, stream: BinaryIO) -> Optional[SyntaxNode]:
        node_class_num = self._read_number(stream)
        node_type_num = self._read_number(stream)
        if node_class_num is None or node_type_num is None:
            return None
        try:
            node_class = NodeClass(node_class_num)
            node_type = SyntaxNodeType(node_type_num)
        except ValueError:
            raise BinarySerializerError('invalid node class or type')
        node = self._create_node(node_class, node_type)

        col = self._read_number(stream)
        line = self._read_number(stream)
        if col is None or line is None:
            return None
        node.col = col
        node.line = line

        if node_class == NodeClass.COMPOUND:
            end_col = self._read_number(stream)
            end_line = self._read_number(stream)
            if end_col is None or end_line is None:
                return None
            node.end_col = end_col
            node.end_line = end_line
        elif node_class == NodeClass.VALUED:
            value = self._read_string(stream)
            if value is None:
                return None
            node.value = value
        elif node_class == NodeClass.COMMENT:
            text = self._read_string(stream)
            if text is None:
                return None
            node.text = text

        attr_count = self._read_number(stream)
        if attr_count is None:
            return None
        for _ in range(attr_count):
            attr_key_num = self._read_number(stream)
            if attr_key_num is None:
                return None
            try:
                attr_key = AttributeName(attr_key_num)
            except ValueError:
                raise BinarySerializerError('invalid attribute key')
            attr_value = self._read_string(stream)
            if attr_value is None:
                return None
            node.set_attribute(attr_key, attr_value)

        child_count = self._read_number(stream)
        if child_count is None:
            return None
        for _ in range(child_count):
            child_node = self._read_node(stream)
            if child_node is None:
                return None
            node.add_child(child_node)

        return node

    def _read_number(self, stream: BinaryIO) -> Optional[int]:
        shift = 0
        num = 0
        while True:
            data = stream.read(1)
            if len(data) != 1:
                return None
            low = data[0]
            num |= (low & 0x7F) << shift
            shift += 7
            if (low & 0x80) == 0:
                return num

    def _read_string(self, stream: BinaryIO) -> Optional[str]:
        length = self._read_number(stream)
        if length is None:
            return None
        if (length >> 24) == 0xFF:
            string_id = length & 0x00FFFFFF
            if string_id >= len(self._string_list):
                return None
            return self._string_list[string_id]
        data = stream.read(length) if length else b''
        if len(data) != length:
            return None
        value = data.decode('utf-8')
        if len(value) > 4:
            self._string_list.append(value)
        return value

    def _write_node(self, stream: BinaryIO, node: SyntaxNode) -> bool:
        if isinstance(node, CompoundSyntaxNode):
            node_class = NodeClass.COMPOUND
        elif isinstance(node, ValuedSyntaxNode):
            node_class = NodeClass.VALUED
        elif isinstance(node, CommentNode):
            node_class = NodeClass.COMMENT
        else:
            node_class = NodeClass.SYNTAX

        if not self._write_number(stream, int(node_class)):
            return False
        if not self._write_number(stream, int(node.typ)):
            return False
        if not self._write_number(stream, node.col):
            return False
        if not self._write_number(stream, node.line):
            return False

        if node_class == NodeClass.COMPOUND:
            if not self._write_number(stream, node.end_col):
                return False
            if not self._write_number(stream, node.end_line):
                return False
        elif node_class == NodeClass.VALUED:
            if not self._write_string(stream, node.value):
                return False
        elif node_class == NodeClass.COMMENT:
            if not self._write_string(stream, node.text):
                return False

        if not self._write_number(stream, len(node.attributes)):
            return False
        for attr_key, attr_value in node.attributes:
            if not self._write_number(stream, int(attr_key)):
                return False
            if not self._write_string(stream, attr_value):
                return False

        if not self._write_number(stream, len(node.child_nodes)):
            return False
        for child_node in node.child_nodes:
            if not self._write_node(stream, child_node):
                return False

        return True

    def _write_number(self, stream: BinaryIO, num: int) -> bool:
        value = num
        while True:
            low = value & 0x7F
            value >>= 7
            if value != 0:
                low |= 0x80
            if stream.write(bytes([low])) != 1:
                return False
            if value == 0:
                return True

    def _write_string(self, stream: BinaryIO, value: str) -> bool:
        if len(value) > 4 and value in self._string_table:
            string_id = self._string_table[value]
            return self._write_number(stream, string_id | 0xFF000000)

        if len(value) > 4:
            self._string_table[value] = len(self._string_table)
            if self._string_table[value] > 0xFFFFFF:
                raise BinarySerializerError('too many strings')

        data = value.encode('utf-8')
        if not self._write_number(stream, len(data)):
            return False
        if data:
            return stream.write(data) == len(data)
        return True


def loads(data: bytes) -> SyntaxNode:
    return BinarySerializer().read(io.BytesIO(data))


def dumps(node: SyntaxNode) -> bytes:
    stream = io.BytesIO()
    BinarySerializer().write(stream, node)
    return stream.getvalue()
