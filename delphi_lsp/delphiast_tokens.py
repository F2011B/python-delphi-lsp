"""Token model for the pure-Python DelphiAST parser.

This file is a Python port inspired by DelphiAST/SimpleParser, licensed under
MPL-2.0: https://github.com/RomanYankovsky/DelphiAST
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenKind(str, Enum):
    IDENTIFIER = "identifier"
    KEYWORD = "keyword"
    NUMBER = "number"
    STRING = "string"
    CHAR_CODE = "char_code"
    SYMBOL = "symbol"
    COMMENT = "comment"
    DIRECTIVE = "directive"
    UNKNOWN = "unknown"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    line: int
    column: int
    start: int
    end: int
    file_name: str

    @property
    def normalized(self) -> str:
        return self.value.casefold()


__all__ = ["Token", "TokenKind"]
