"""Parser backend selection for the DelphiAST Python migration.

The DelphiAST-compatible implementation is the default.  The legacy Lark
backend remains available explicitly while compatibility coverage is expanded.
"""

from __future__ import annotations

from enum import Enum

from .delphiast_parser import DelphiAstParseOutput, DelphiAstParser
from .nodes import SyntaxNode


class ParserBackend(str, Enum):
    DELPHIAST = "delphiast"
    HYBRID = "hybrid"
    LARK = "lark"


class ParserMode(str, Enum):
    TOLERANT = "tolerant"
    STRICT = "strict"


def normalize_backend(value: ParserBackend | str) -> ParserBackend:
    try:
        return value if isinstance(value, ParserBackend) else ParserBackend(value.casefold())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"Unsupported parser backend: {value!r}.") from error


def normalize_mode(value: ParserMode | str) -> ParserMode:
    try:
        return value if isinstance(value, ParserMode) else ParserMode(value.casefold())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"Unsupported parser mode: {value!r}.") from error


def parse_delphiast_syntax_tree(text: str, file_name: str) -> DelphiAstParseOutput:
    return DelphiAstParser(text, file_name).parse()


def build_delphiast_syntax_tree(text: str, file_name: str) -> SyntaxNode:
    """Compatibility helper for callers that only need the root node."""

    return parse_delphiast_syntax_tree(text, file_name).root


__all__ = [
    "ParserBackend",
    "ParserMode",
    "build_delphiast_syntax_tree",
    "normalize_backend",
    "normalize_mode",
    "parse_delphiast_syntax_tree",
]
