from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable, Optional

from .comment_builder import build_comment_nodes
from .grammar import build_grammar
from .lark_builder import build_syntax_tree
from .nodes import SyntaxNode
from .preprocessor import IncludeLoader, PreprocessedSource, Preprocessor, PreprocessorOptions
from .semantic import SymbolIndex
from .semantic_builder import SemanticBuilder, SemanticModel

StringTransform = Callable[[str], str]


@dataclass
class ParseResult:
    root: SyntaxNode
    comments: list
    preprocessed: PreprocessedSource
    semantic: Optional[SemanticModel] = None


class DelphiParser:
    def __init__(
        self,
        *,
        include_paths: Iterable[str] = (),
        defines: Iterable[str] = (),
        include_loader: IncludeLoader | None = None,
        preprocessor_options: Optional[PreprocessorOptions] = None,
        interface_only: bool = False,
        on_handle_string: StringTransform | None = None,
    ) -> None:
        self._include_paths = list(include_paths)
        self._defines = list(defines)
        self._include_loader = include_loader
        self._preprocessor_options = preprocessor_options
        self._interface_only = interface_only
        self._on_handle_string = on_handle_string

    def parse(
        self,
        text: str,
        file_name: str,
        *,
        build_semantic: bool = False,
        index: SymbolIndex | None = None,
        include_loader: IncludeLoader | None = None,
        interface_only: bool | None = None,
        on_handle_string: StringTransform | None = None,
    ) -> ParseResult:
        effective_include_loader = include_loader if include_loader is not None else self._include_loader
        effective_interface_only = self._interface_only if interface_only is None else interface_only
        effective_string_hook = on_handle_string if on_handle_string is not None else self._on_handle_string

        preprocessor = Preprocessor(
            defines=self._defines,
            include_paths=self._include_paths,
            include_loader=effective_include_loader,
            options=self._preprocessor_options,
        )
        preprocessed = preprocessor.process(text, file_name)
        parse_text = self._to_interface_only(preprocessed.text) if effective_interface_only else preprocessed.text
        tree = self._parse_lark(parse_text)
        root = build_syntax_tree(tree, file_name, string_transform=effective_string_hook)
        comments = build_comment_nodes(preprocessed.comments)
        semantic = SemanticBuilder().build(root, index=index) if build_semantic else None
        return ParseResult(root=root, comments=comments, preprocessed=preprocessed, semantic=semantic)

    def _parse_lark(self, text: str):
        parser = _get_lark_parser()
        return parser.parse(text)

    def _to_interface_only(self, text: str) -> str:
        if not text:
            return text
        lower = text.casefold().lstrip()
        if not lower.startswith('unit'):
            return text

        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ''

            if ch == "'" and not (ch == '{' or (ch == '(' and nxt == '*')):
                i += 1
                while i < n:
                    if text[i] == "'":
                        if i + 1 < n and text[i + 1] == "'":
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                continue

            if ch == '/' and nxt == '/':
                i += 2
                while i < n and text[i] not in {'\n', '\r'}:
                    i += 1
                continue

            if ch == '{':
                i += 1
                while i < n and text[i] != '}':
                    i += 1
                if i < n:
                    i += 1
                continue

            if ch == '(' and nxt == '*':
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i + 1] == ')'):
                    i += 1
                if i + 1 < n:
                    i += 2
                continue

            if ch.isalpha() or ch == '_':
                start = i
                i += 1
                while i < n and (text[i].isalnum() or text[i] == '_'):
                    i += 1
                word = text[start:i]
                if word.casefold() == 'implementation':
                    return text[:start] + '\nend.\n'
                continue

            i += 1
        return text


@lru_cache(maxsize=1)
def _get_lark_parser():
    try:
        from lark import Lark
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError('lark is required to parse delphi source') from exc
    grammar = build_grammar()
    return Lark(
        grammar,
        parser='lalr',
        lexer='contextual',
        propagate_positions=True,
        maybe_placeholders=False,
    )


def parse(
    text: str,
    file_name: str,
    *,
    include_paths: Iterable[str] = (),
    defines: Iterable[str] = (),
    include_loader: IncludeLoader | None = None,
    preprocessor_options: Optional[PreprocessorOptions] = None,
    build_semantic: bool = False,
    index: SymbolIndex | None = None,
    interface_only: bool = False,
    on_handle_string: StringTransform | None = None,
) -> ParseResult:
    parser = DelphiParser(
        include_paths=include_paths,
        defines=defines,
        include_loader=include_loader,
        preprocessor_options=preprocessor_options,
        interface_only=interface_only,
        on_handle_string=on_handle_string,
    )
    return parser.parse(
        text,
        file_name,
        build_semantic=build_semantic,
        index=index,
        interface_only=interface_only,
        on_handle_string=on_handle_string,
    )
