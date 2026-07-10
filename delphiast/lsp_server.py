from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse
import os
import re

from .semantic import (
    ReferenceKind,
    Scope,
    ScopeKind,
    SourceRange,
    Symbol,
    SymbolIndex,
    SymbolKind,
    SymbolReference,
    TypeRef,
    NamedTypeRef,
    GenericInstanceTypeRef,
    UnknownTypeRef,
    Visibility,
)
from .semantic_builder import SemanticBuilder, SemanticModel
from .source_reader import read_source_text
from .workspace import WorkspaceSemanticResult, build_workspace_semantics
from .project_discovery import discover_delphi_project


@dataclass
class DocumentSnapshot:
    uri: str
    file_name: str
    text: str
    semantic: Optional[SemanticModel] = None


@dataclass
class FileSnapshot:
    path: str
    text: str
    mtime: float


@dataclass
class WorkspaceConfig:
    roots: list[str] = field(default_factory=list)
    include_paths: list[str] = field(default_factory=list)
    search_paths: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    extensions: tuple[str, ...] = ('.pas', '.dpr', '.dpk', '.inc')
    outline_line_threshold: int = 50_000
    eager_index: bool = False
    auto_discover_paths: bool = True
    discovered_include_paths: list[str] = field(default_factory=list)
    discovered_search_paths: list[str] = field(default_factory=list)
    discovered_defines: list[str] = field(default_factory=list)


@dataclass
class LspWorkspaceState:
    config: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    documents: dict[str, DocumentSnapshot] = field(default_factory=dict)
    workspace: Optional[WorkspaceSemanticResult] = None
    workspace_symbol_index: Optional[WorkspaceSemanticResult] = None
    workspace_symbol_query_cache: dict[str, WorkspaceSemanticResult] = field(default_factory=dict)
    workspace_files: set[str] = field(default_factory=set)
    file_cache: dict[str, FileSnapshot] = field(default_factory=dict)

    def configure(self, config: WorkspaceConfig) -> None:
        self.config = self._with_discovered_config(config)
        self.workspace_files = set()
        self.file_cache = {}
        self.workspace = None
        self.workspace_symbol_index = None
        self.workspace_symbol_query_cache = {}
        if config.eager_index:
            self.index_workspace()

    def _with_discovered_config(self, config: WorkspaceConfig) -> WorkspaceConfig:
        if not config.auto_discover_paths or not config.roots:
            return config
        include_paths = list(config.include_paths)
        search_paths = list(config.search_paths)
        defines = list(config.defines)
        discovered_include_paths: list[str] = []
        discovered_search_paths: list[str] = []
        discovered_defines: list[str] = []

        def add_path(target: list[str], discovered: list[str], value: str) -> None:
            normalized = str(Path(value).expanduser().resolve())
            if normalized not in target:
                target.append(normalized)
            if normalized not in discovered:
                discovered.append(normalized)

        def add_define(value: str) -> None:
            if value not in defines:
                defines.append(value)
            if value not in discovered_defines:
                discovered_defines.append(value)

        for root in config.roots:
            discovery = discover_delphi_project(root, include_paths=config.include_paths, search_paths=config.search_paths, defines=config.defines)
            for path in discovery.include_paths:
                add_path(include_paths, discovered_include_paths, path)
            for path in discovery.search_paths:
                add_path(search_paths, discovered_search_paths, path)
            for define in discovery.defines:
                add_define(define)
        return WorkspaceConfig(
            roots=list(config.roots),
            include_paths=include_paths,
            search_paths=search_paths,
            defines=defines,
            extensions=config.extensions,
            outline_line_threshold=config.outline_line_threshold,
            eager_index=config.eager_index,
            auto_discover_paths=config.auto_discover_paths,
            discovered_include_paths=discovered_include_paths,
            discovered_search_paths=discovered_search_paths,
            discovered_defines=discovered_defines,
        )

    def index_workspace(self) -> None:
        self.workspace_files = set(self._scan_workspace_files())
        self._refresh_file_cache()
        self._rebuild()

    def ensure_workspace_indexed(self) -> None:
        if self.workspace is None and self.config.roots:
            self.index_workspace()

    def ensure_workspace_symbol_indexed(self) -> None:
        if self.workspace_symbol_index is not None:
            return
        if not self.workspace_files and self.config.roots:
            self.workspace_files = set(self._scan_workspace_files())
        self._refresh_file_cache()
        sources = {path: snapshot.text for path, snapshot in self.file_cache.items()}
        for doc in self.documents.values():
            sources[doc.file_name] = doc.text
        self.workspace_symbol_index = self._outline_workspace_semantics(sources, require_all_large=False)

    def workspace_symbols_for_query(self, query: str) -> Optional[WorkspaceSemanticResult]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            self.ensure_workspace_symbol_indexed()
            return self.workspace_symbol_index
        if self.workspace_symbol_index is not None:
            return self.workspace_symbol_index
        cached = self.workspace_symbol_query_cache.get(normalized_query)
        if cached is not None:
            return cached
        if not self.workspace_files and self.config.roots:
            self.workspace_files = set(self._scan_workspace_files())
        self._refresh_file_cache()
        sources = {
            path: snapshot.text
            for path, snapshot in self.file_cache.items()
            if normalized_query in snapshot.text.casefold()
        }
        for doc in self.documents.values():
            if normalized_query in doc.text.casefold():
                sources[doc.file_name] = doc.text
        result = self._outline_workspace_semantics(sources, require_all_large=False)
        if result is None:
            result = WorkspaceSemanticResult(models={}, index=SymbolIndex())
        self.workspace_symbol_query_cache[normalized_query] = result
        return result

    def update_document(self, uri: str, text: str) -> None:
        file_name = uri_to_path(uri)
        self.documents[uri] = DocumentSnapshot(uri=uri, file_name=file_name, text=text)
        self.workspace_symbol_index = None
        self.workspace_symbol_query_cache = {}
        self._rebuild()

    def remove_document(self, uri: str) -> None:
        if uri in self.documents:
            self.documents.pop(uri)
            self.workspace_symbol_index = None
            self.workspace_symbol_query_cache = {}
            self._rebuild()

    def _scan_workspace_files(self) -> list[str]:
        files: list[str] = []
        for root in self.config.roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            for ext in self.config.extensions:
                files.extend(str(path) for path in root_path.rglob(f'*{ext}'))
        return files

    def _refresh_file_cache(self) -> None:
        to_remove = [path for path in self.file_cache if path not in self.workspace_files]
        for path in to_remove:
            self.file_cache.pop(path, None)
        for path in self.workspace_files:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = self.file_cache.get(path)
            if cached is not None and cached.mtime == mtime:
                continue
            try:
                text = read_source_text(Path(path))
            except (OSError, UnicodeError):
                continue
            self.file_cache[path] = FileSnapshot(path=path, text=text, mtime=mtime)

    def _collect_sources(self) -> dict[str, str]:
        sources = {
            path: outline_large_source(snapshot.text, self.config.outline_line_threshold)
            for path, snapshot in self.file_cache.items()
        }
        for doc in self.documents.values():
            sources[doc.file_name] = outline_large_source(doc.text, self.config.outline_line_threshold)
        return sources

    def _rebuild(self) -> None:
        sources = self._collect_sources()
        self.workspace_symbol_index = None
        self.workspace_symbol_query_cache = {}
        if not sources:
            self.workspace = None
            return
        outline_result = self._outline_workspace_semantics(sources)
        if outline_result is not None:
            self.workspace = outline_result
            for doc in self.documents.values():
                doc.semantic = outline_result.models.get(doc.file_name)
            return
        result = build_workspace_semantics(
            sources,
            include_paths=self.config.include_paths,
            defines=self.config.defines,
        )
        self.workspace = result
        for doc in self.documents.values():
            doc.semantic = result.models.get(doc.file_name)

    def semantic_for_uri(self, uri: str) -> Optional[SemanticModel]:
        doc = self.documents.get(uri)
        if doc is not None:
            return doc.semantic
        file_name = uri_to_path(uri)
        model = self.model_for_path(file_name)
        if model is not None:
            return model
        if '://' in file_name:
            return None
        path = Path(file_name)
        if not path.exists():
            return None
        try:
            text = read_source_text(path)
        except (OSError, UnicodeError):
            return None
        if is_large_source(text, self.config.outline_line_threshold):
            return build_outline_semantic_model(text, file_name)
        result = build_workspace_semantics(
            {file_name: outline_large_source(text, self.config.outline_line_threshold)},
            include_paths=self.config.include_paths,
            defines=self.config.defines,
        )
        return result.models.get(file_name)

    def structure_semantic_for_uri(self, uri: str) -> Optional[SemanticModel]:
        text = self.text_for_uri(uri)
        if text is None:
            return None
        return build_outline_semantic_model(text, uri_to_path(uri))

    def text_for_uri(self, uri: str) -> Optional[str]:
        doc = self.documents.get(uri)
        if doc is not None:
            return doc.text
        file_name = uri_to_path(uri)
        cached = self.file_cache.get(file_name)
        if cached is not None:
            return cached.text
        if '://' in file_name:
            return None
        path = Path(file_name)
        if not path.exists():
            return None
        try:
            return read_source_text(path)
        except (OSError, UnicodeError):
            return None

    def _outline_workspace_semantics(
        self,
        sources: dict[str, str],
        *,
        require_all_large: bool = True,
    ) -> Optional[WorkspaceSemanticResult]:
        if not sources:
            return None
        if require_all_large and not all(is_large_source(text, self.config.outline_line_threshold) for text in sources.values()):
            return None
        models = {file_name: build_outline_semantic_model(text, file_name) for file_name, text in sources.items()}
        index = SymbolIndex()
        for model in models.values():
            index.register_unit(model.unit_scope.name, model.unit_scope)
        for model in models.values():
            model.index = index
        return WorkspaceSemanticResult(models=models, index=index)

    def model_for_path(self, path: str) -> Optional[SemanticModel]:
        if self.workspace is None:
            return None
        return self.workspace.models.get(path)

    def iter_models(self) -> Iterable[SemanticModel]:
        if self.workspace is None:
            return []
        return self.workspace.models.values()

    def uri_for_file_name(self, file_name: str) -> Optional[str]:
        for doc in self.documents.values():
            if doc.file_name == file_name:
                return doc.uri
        return None

    def diagnostics_for_uri(self, uri: str):
        model = self.semantic_for_uri(uri)
        return diagnostics_for_model(model) if model else []


BUILTIN_TYPES = {
    'string', 'ansistring', 'widestring', 'unicodestring',
    'char', 'widechar',
    'integer', 'smallint', 'shortint', 'longint', 'int64', 'uint64',
    'byte', 'word', 'cardinal', 'nativeint', 'nativeuint',
    'boolean', 'bytebool', 'wordbool', 'longbool',
    'single', 'double', 'extended', 'currency', 'real', 'real48',
    'variant', 'olevariant', 'pointer', 'pchar', 'tobject', 'tclass',
}


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != 'file':
        return uri
    path = unquote(parsed.path)
    if path.startswith('/') and len(path) > 3 and path[2] == ':':
        path = path[1:]
    return path or uri


def outline_large_source(text: str, line_threshold: int) -> str:
    if not is_large_source(text, line_threshold):
        return text
    return outline_source(text)


def outline_source(text: str) -> str:
    return _blank_compound_statement_bodies(text)


def multiline_string_block_end(text: str, start: int) -> int | None:
    for quote_count in (5, 3):
        delimiter = "'" * quote_count
        if not text.startswith(delimiter, start):
            continue
        content_start = start + quote_count
        if text.startswith('\r\n', content_start):
            content_start += 2
        elif content_start < len(text) and text[content_start] == '\n':
            content_start += 1
        else:
            continue
        close = text.find(delimiter, content_start)
        return len(text) if close < 0 else close + quote_count
    return None


def is_large_source(text: str, line_threshold: int) -> bool:
    return line_threshold > 0 and text.count('\n') + 1 > line_threshold


# These are the statement grammar alternatives that close with END.
_END_TERMINATED_STATEMENTS = frozenset({'begin', 'asm', 'case', 'try'})
# Inline variable type_spec declarations can place these inside a body.
_END_TERMINATED_STRUCTURED_TYPES = frozenset(
    {'class', 'record', 'object', 'interface', 'dispinterface'}
)
_CLASS_MEMBER_PREFIX_FOLLOWERS = frozenset(
    {
        'const',
        'constructor',
        'destructor',
        'function',
        'of',
        'operator',
        'procedure',
        'property',
        'threadvar',
        'type',
        'var',
    }
)
_CLASS_TYPE_PREDECESSORS = frozenset({':', '=', 'of', 'packed', 'to', 'type', '^'})
_CLASS_MEMBER_BOUNDARIES = frozenset(
    {
        ';',
        ')',
        ']',
        'automated',
        'class',
        'dispinterface',
        'interface',
        'object',
        'private',
        'protected',
        'public',
        'published',
        'record',
    }
)
_GENERIC_ROUTINE_HEADINGS = frozenset(
    {'constructor', 'destructor', 'function', 'operator', 'procedure'}
)


def _blank_compound_statement_bodies(text: str) -> str:
    chars = list(text)
    end_stack: list[str] = []
    body_start: int | None = None
    previous_token: str | None = None
    previous_token_is_identifier = False
    paren_depth = 0
    bracket_depth = 0
    angle_depth = 0
    generic_owner_ready = False
    routine_heading_active = False
    type_constructors: dict[tuple[int, int], str] = {}
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        if end_stack and (
            (ch == '{' and nxt == '$')
            or (ch == '(' and nxt == '*' and i + 2 < n and text[i + 2] == '$')
        ):
            # Raw conditional branches can disagree on END nesting, so keep the file all-or-safe.
            return text

        if ch.isspace():
            i += 1
            while i < n and text[i].isspace():
                i += 1
            continue

        if ch == "'":
            block_end = multiline_string_block_end(text, i)
            if block_end is not None:
                i = block_end
                previous_token = 'literal'
                previous_token_is_identifier = False
                generic_owner_ready = False
                continue
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            previous_token = 'literal'
            previous_token_is_identifier = False
            generic_owner_ready = False
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

        if ch == '&' and (nxt.isalpha() or nxt == '_'):
            start = i
            i += 2
            while i < n and (text[i].isalnum() or text[i] == '_'):
                i += 1
            previous_token = text[start:i].casefold()
            previous_token_is_identifier = True
            generic_owner_ready = False
            continue

        if ch.isalpha() or ch == '_':
            start = i
            i += 1
            while i < n and (text[i].isalnum() or text[i] == '_'):
                i += 1
            word = text[start:i].casefold()
            generic_owner_ready = False
            opens_construct: bool | None = False
            if not end_stack:
                if word in {'begin', 'asm'}:
                    body_start = i
                    end_stack.append(word)
            elif word == 'end':
                end_stack.pop()
                if not end_stack and body_start is not None:
                    _blank_preserving_newlines(chars, body_start, start)
                    body_start = None
            else:
                if angle_depth > 0 and word in _END_TERMINATED_STRUCTURED_TYPES:
                    # Constraint keywords are names inside a confirmed generic angle.
                    opens_construct = False
                else:
                    opens_construct = _opens_end_terminated_construct(
                        word,
                        text=text,
                        word_end=i,
                        previous_token=previous_token,
                        end_stack=end_stack,
                        type_constructor=type_constructors.get((paren_depth, bracket_depth)),
                    )
                if opens_construct is None:
                    return text
                if opens_construct:
                    end_stack.append(word)
            if word == 'class' and opens_construct:
                generic_owner_ready = True
            if word in _GENERIC_ROUTINE_HEADINGS:
                routine_heading_active = True
            if word in {'begin', 'asm'}:
                type_constructors.clear()
                routine_heading_active = False
            elif word in {'procedure', 'function', 'array'}:
                type_constructors[(paren_depth, bracket_depth)] = word
            elif word == 'var':
                type_constructors.pop((paren_depth, bracket_depth), None)
            previous_token = word
            previous_token_is_identifier = True
            continue

        context_key = (paren_depth, bracket_depth)
        if ch == '<' and nxt not in {'=', '>'}:
            if angle_depth > 0:
                if not previous_token_is_identifier:
                    return text
                angle_depth += 1
            elif generic_owner_ready or (
                routine_heading_active
                and previous_token_is_identifier
                and previous_token != 'operator'
            ):
                angle_depth = 1
            generic_owner_ready = False
        elif ch == '>' and nxt != '=' and angle_depth > 0:
            angle_depth -= 1
            generic_owner_ready = False
        elif ch == '(':
            paren_depth += 1
            generic_owner_ready = False
            routine_heading_active = False
        elif ch == ')':
            type_constructors.pop(context_key, None)
            paren_depth = max(0, paren_depth - 1)
            generic_owner_ready = False
        elif ch == '[':
            bracket_depth += 1
            generic_owner_ready = False
        elif ch == ']':
            type_constructors.pop(context_key, None)
            bracket_depth = max(0, bracket_depth - 1)
            generic_owner_ready = False
        elif ch == ';':
            type_constructors.pop(context_key, None)
            generic_owner_ready = False
            routine_heading_active = False
        elif ch not in {'<', '>'}:
            generic_owner_ready = False
        previous_token = ch
        previous_token_is_identifier = False
        i += 1
    return text if angle_depth > 0 else ''.join(chars)


def _opens_end_terminated_construct(
    word: str,
    *,
    text: str,
    word_end: int,
    previous_token: str | None,
    end_stack: list[str],
    type_constructor: str | None,
) -> bool | None:
    if word in _END_TERMINATED_STRUCTURED_TYPES:
        if word == 'object' and previous_token == 'of':
            if type_constructor in {'procedure', 'function'}:
                return False
            if type_constructor == 'array':
                return True
            return None
        if word == 'class':
            next_word = _next_code_word(text, word_end)
            if next_word == 'of':
                return False
            if (
                end_stack[-1] in _END_TERMINATED_STRUCTURED_TYPES
                and next_word in _CLASS_MEMBER_PREFIX_FOLLOWERS
            ):
                if previous_token in _CLASS_TYPE_PREDECESSORS:
                    return True
                if previous_token in _CLASS_MEMBER_BOUNDARIES:
                    return False
                return None
        return True
    if end_stack[-1] in _END_TERMINATED_STRUCTURED_TYPES:
        return False
    return word in _END_TERMINATED_STATEMENTS


def _next_code_word(text: str, start: int) -> str | None:
    i = start
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith('//', i):
            i += 2
            while i < n and text[i] not in {'\r', '\n'}:
                i += 1
            if i < n and text[i] == '\r' and i + 1 < n and text[i + 1] == '\n':
                i += 2
            elif i < n:
                i += 1
            continue
        if text[i] == '{':
            close = text.find('}', i + 1)
            i = n if close < 0 else close + 1
            continue
        if text.startswith('(*', i):
            close = text.find('*)', i + 2)
            i = n if close < 0 else close + 2
            continue
        break
    if i >= n or not (text[i].isalpha() or text[i] == '_'):
        return None
    end = i + 1
    while end < n and (text[end].isalnum() or text[end] == '_'):
        end += 1
    return text[i:end].casefold()


def _blank_preserving_newlines(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] not in {'\n', '\r'}:
            chars[index] = ' '


_IDENTIFIER = r'[A-Za-z_][A-Za-z0-9_]*'
_QUALIFIED_IDENTIFIER = rf'{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*'
_UNIT_RE = re.compile(rf'^\s*(?:unit|program|library|package)\s+({_QUALIFIED_IDENTIFIER})\b', re.IGNORECASE)
_TYPE_RE = re.compile(rf'^\s*({_IDENTIFIER})(?:\s*<[^;=]+>)?\s*=\s*(.*)', re.IGNORECASE)
_ROUTINE_RE = re.compile(
    rf'^\s*(?:class\s+)?(procedure|function|constructor|destructor|operator)\s+({_QUALIFIED_IDENTIFIER})\b',
    re.IGNORECASE,
)
_VARIABLE_RE = re.compile(rf'^\s*({_IDENTIFIER})\s*:\s*([^;=]+)', re.IGNORECASE)
_PROPERTY_RE = re.compile(rf'^\s*property\s+({_IDENTIFIER})\b(?:[^:;]*:\s*([^;]+))?', re.IGNORECASE)
_CONSTANT_RE = re.compile(rf'^\s*({_IDENTIFIER})\s*(?::[^=]+)?=', re.IGNORECASE)
_SECTION_RE = re.compile(
    r'^\s*(type|var|const|threadvar|resourcestring|implementation|initialization|finalization|begin|end)\b',
    re.IGNORECASE,
)
_VISIBILITY_RE = re.compile(
    r'^\s*(strict\s+private|strict\s+protected|private|protected|public|published)\b',
    re.IGNORECASE,
)
_TYPE_REF_STOP_RE = re.compile(
    r'\b(read|write|add|remove|stored|default|nodefault|index|implements)\b',
    re.IGNORECASE,
)


def build_outline_semantic_model(text: str, file_name: str) -> SemanticModel:
    unit_name = _outline_unit_name(text, file_name)
    unit_scope = Scope(kind=ScopeKind.UNIT, name=unit_name)
    unit_range = SourceRange(file_name, 1, 1, 1, max(2, len(unit_name) + 1))
    unit_symbol = Symbol(
        name=unit_name,
        kind=SymbolKind.UNIT,
        decl_range=unit_range,
        name_range=unit_range,
        scope=unit_scope,
    )
    unit_scope.owner = unit_symbol
    unit_scope.define(unit_symbol)

    comment_state: str | None = None
    section = ''
    current_routine: Symbol | None = None
    current_routine_scope: Scope | None = None
    routine_var_section = False
    current_type: Symbol | None = None
    current_type_scope: Scope | None = None
    type_visibility = Visibility.PUBLIC

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line, comment_state = _mask_delphi_comments_and_strings(raw_line, comment_state)
        stripped = line.strip()
        if not stripped:
            continue

        if section == 'type' and current_type_scope is not None:
            section_match = _SECTION_RE.match(line)
            if section_match and section_match.group(1).casefold() == 'end':
                if current_type is not None:
                    current_type.decl_range = SourceRange(
                        file_name,
                        current_type.decl_range.start_line,
                        current_type.decl_range.start_col,
                        line_number,
                        max(1, len(raw_line) + 1),
                    )
                current_type = None
                current_type_scope = None
                type_visibility = Visibility.PUBLIC
                continue

            visibility_match = _VISIBILITY_RE.match(line)
            if visibility_match:
                type_visibility = _outline_visibility(visibility_match.group(1))
                continue

            routine_match = _ROUTINE_RE.match(line)
            if routine_match:
                keyword = routine_match.group(1).casefold()
                _define_outline_symbol(
                    current_type_scope,
                    file_name,
                    _normalize_outline_member_name(routine_match.group(2)),
                    _routine_symbol_kind(keyword),
                    line_number,
                    routine_match.start(2),
                    routine_match.end(2),
                    visibility=type_visibility,
                )
                continue

            property_match = _PROPERTY_RE.match(line)
            if property_match:
                _define_outline_symbol(
                    current_type_scope,
                    file_name,
                    property_match.group(1),
                    SymbolKind.PROPERTY,
                    line_number,
                    property_match.start(1),
                    property_match.end(1),
                    visibility=type_visibility,
                    type_ref=_outline_type_ref(property_match.group(2) or ''),
                )
                continue

            field_match = _VARIABLE_RE.match(line)
            if field_match:
                _define_outline_symbol(
                    current_type_scope,
                    file_name,
                    field_match.group(1),
                    SymbolKind.FIELD,
                    line_number,
                    field_match.start(1),
                    field_match.end(1),
                    visibility=type_visibility,
                    type_ref=_outline_type_ref(field_match.group(2)),
                )
                continue

        routine_match = _ROUTINE_RE.match(line) if section != 'type' else None
        if routine_match:
            keyword = routine_match.group(1).casefold()
            name = _normalize_outline_name(routine_match.group(2))
            current_routine = _define_outline_symbol(
                unit_scope,
                file_name,
                name,
                _routine_symbol_kind(keyword),
                line_number,
                routine_match.start(2),
                routine_match.end(2),
            )
            current_routine_scope = Scope(
                kind=ScopeKind.ROUTINE,
                name=name,
                parent=unit_scope,
                owner=current_routine,
            )
            current_routine.member_scope = current_routine_scope
            routine_var_section = False
            section = ''
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            keyword = section_match.group(1).casefold()
            if current_routine is not None:
                if keyword in {'var', 'threadvar'}:
                    routine_var_section = True
                    continue
                if keyword == 'begin':
                    routine_var_section = False
                    continue
                if keyword == 'end':
                    current_routine.decl_range = SourceRange(
                        file_name,
                        current_routine.decl_range.start_line,
                        current_routine.decl_range.start_col,
                        line_number,
                        max(1, len(raw_line) + 1),
                    )
                    current_routine = None
                    current_routine_scope = None
                    routine_var_section = False
                    continue
            if keyword == 'type':
                section = 'type'
            elif keyword in {'var', 'threadvar', 'const', 'resourcestring'}:
                section = keyword
            elif keyword in {'implementation', 'initialization', 'finalization', 'begin'}:
                section = ''
            continue

        if current_routine_scope is not None and routine_var_section:
            var_match = _VARIABLE_RE.match(line)
            if var_match:
                _define_outline_symbol(
                    current_routine_scope,
                    file_name,
                    var_match.group(1),
                    SymbolKind.VARIABLE,
                    line_number,
                    var_match.start(1),
                    var_match.end(1),
                    type_ref=_outline_type_ref(var_match.group(2)),
                )
            continue

        if section == 'type':
            type_match = _TYPE_RE.match(line)
            if type_match:
                kind = _type_symbol_kind(type_match.group(2))
                type_symbol = _define_outline_symbol(
                    unit_scope,
                    file_name,
                    type_match.group(1),
                    kind,
                    line_number,
                    type_match.start(1),
                    type_match.end(1),
                )
                if kind in {SymbolKind.CLASS, SymbolKind.RECORD, SymbolKind.INTERFACE} and not _is_forward_outline_type(type_match.group(2)):
                    current_type = type_symbol
                    current_type_scope = Scope(
                        kind=ScopeKind.TYPE,
                        name=type_symbol.name,
                        parent=unit_scope,
                        owner=type_symbol,
                    )
                    type_symbol.member_scope = current_type_scope
                    type_visibility = Visibility.PUBLIC
            continue

        if section in {'const', 'resourcestring'}:
            const_match = _CONSTANT_RE.match(line)
            if const_match:
                _define_outline_symbol(
                    unit_scope,
                    file_name,
                    const_match.group(1),
                    SymbolKind.CONSTANT,
                    line_number,
                    const_match.start(1),
                    const_match.end(1),
                )
            continue

        if section in {'var', 'threadvar'}:
            var_match = _VARIABLE_RE.match(line)
            if var_match:
                _define_outline_symbol(
                    unit_scope,
                    file_name,
                    var_match.group(1),
                    SymbolKind.VARIABLE,
                    line_number,
                    var_match.start(1),
                    var_match.end(1),
                    type_ref=_outline_type_ref(var_match.group(2)),
                )

    index = SymbolIndex()
    index.register_unit(unit_name, unit_scope)
    return SemanticModel(unit_scope=unit_scope, index=index)


def _outline_unit_name(text: str, file_name: str) -> str:
    comment_state: str | None = None
    for raw_line in text.splitlines()[:200]:
        line, comment_state = _mask_delphi_comments_and_strings(raw_line, comment_state)
        match = _UNIT_RE.match(line)
        if match:
            return _normalize_outline_name(match.group(1))
    return Path(file_name).stem or 'unit'


def _define_outline_symbol(
    scope: Scope,
    file_name: str,
    name: str,
    kind: SymbolKind,
    line_number: int,
    start_col_0: int,
    end_col_0: int,
    *,
    visibility: Visibility = Visibility.UNKNOWN,
    type_ref: TypeRef | None = None,
) -> Symbol:
    symbol_range = SourceRange(
        file_name,
        line_number,
        start_col_0 + 1,
        line_number,
        max(start_col_0 + 2, end_col_0 + 1),
    )
    symbol = Symbol(
        name=name,
        kind=kind,
        decl_range=symbol_range,
        name_range=symbol_range,
        scope=scope,
        visibility=visibility,
        type_ref=type_ref or UnknownTypeRef(),
    )
    scope.define(symbol)
    return symbol


def _normalize_outline_name(name: str) -> str:
    return re.sub(r'\s*\.\s*', '.', name.strip())


def _normalize_outline_member_name(name: str) -> str:
    return _normalize_outline_name(name).split('.')[-1]


def _outline_visibility(name: str) -> Visibility:
    normalized = name.casefold().replace(' ', '_')
    mapping = {
        'private': Visibility.PRIVATE,
        'protected': Visibility.PROTECTED,
        'public': Visibility.PUBLIC,
        'published': Visibility.PUBLISHED,
        'strict_private': Visibility.STRICT_PRIVATE,
        'strict_protected': Visibility.STRICT_PROTECTED,
    }
    return mapping.get(normalized, Visibility.UNKNOWN)


def _outline_type_ref(type_text: str) -> TypeRef:
    cleaned = type_text.strip()
    if not cleaned:
        return UnknownTypeRef()
    stop_match = _TYPE_REF_STOP_RE.search(cleaned)
    if stop_match:
        cleaned = cleaned[:stop_match.start()].strip()
    cleaned = cleaned.rstrip(';').strip()
    if not cleaned:
        return UnknownTypeRef()
    return SemanticBuilder()._type_ref_from_name(cleaned)


def _is_forward_outline_type(type_text: str) -> bool:
    rhs = type_text.strip().casefold()
    return rhs in {'class;', 'record;', 'interface;'} or rhs.endswith(' class;') or rhs.endswith(' interface;')


def _routine_symbol_kind(keyword: str) -> SymbolKind:
    if keyword == 'procedure':
        return SymbolKind.PROCEDURE
    if keyword == 'constructor':
        return SymbolKind.CONSTRUCTOR
    if keyword == 'destructor':
        return SymbolKind.DESTRUCTOR
    return SymbolKind.FUNCTION


def _type_symbol_kind(type_text: str) -> SymbolKind:
    rhs = type_text.lstrip().casefold()
    if rhs.startswith('packed '):
        rhs = rhs[7:].lstrip()
    if rhs.startswith('class') and not rhs.startswith('class of'):
        return SymbolKind.CLASS
    if rhs.startswith('record'):
        return SymbolKind.RECORD
    if rhs.startswith('interface'):
        return SymbolKind.INTERFACE
    if rhs.startswith('('):
        return SymbolKind.ENUM
    return SymbolKind.TYPE


def _mask_delphi_comments_and_strings(line: str, state: str | None) -> tuple[str, str | None]:
    chars = list(line)
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ''

        if state == 'brace':
            chars[i] = ' '
            if ch == '}':
                state = None
            i += 1
            continue

        if state == 'paren':
            chars[i] = ' '
            if ch == '*' and nxt == ')':
                chars[i + 1] = ' '
                state = None
                i += 2
            else:
                i += 1
            continue

        if ch == "'":
            chars[i] = ' '
            i += 1
            while i < n:
                chars[i] = ' '
                if line[i] == "'":
                    if i + 1 < n and line[i + 1] == "'":
                        chars[i + 1] = ' '
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        if ch == '/' and nxt == '/':
            for index in range(i, n):
                chars[index] = ' '
            break

        if ch == '{':
            chars[i] = ' '
            state = 'brace'
            i += 1
            continue

        if ch == '(' and nxt == '*':
            chars[i] = ' '
            chars[i + 1] = ' '
            state = 'paren'
            i += 2
            continue

        i += 1
    return ''.join(chars), state


def path_to_uri(path: str) -> str:
    if '://' in path:
        return path
    return Path(path).absolute().as_uri()


def find_reference_at_position(
    model: SemanticModel,
    *,
    line: int,
    character: int,
) -> Optional[SymbolReference]:
    line_1 = line + 1
    col_1 = character + 1
    candidates = [
        ref for ref in model.references if ref.ref_range.contains(line_1, col_1)
    ]
    if not candidates:
        same_line = [
            ref for ref in model.references if ref.ref_range.start_line == line_1
        ]
        if not same_line:
            return None
        return min(
            same_line,
            key=lambda ref: abs(ref.ref_range.start_col - col_1),
        )
    return min(candidates, key=_reference_span)


def find_symbol_at_position(
    model: SemanticModel,
    *,
    line: int,
    character: int,
) -> Optional[Symbol]:
    line_1 = line + 1
    col_1 = character + 1
    candidates: list[Symbol] = []
    for symbol in iter_symbols(model.unit_scope):
        if symbol.name_range.contains(line_1, col_1):
            candidates.append(symbol)
    if not candidates:
        return None
    return min(candidates, key=lambda sym: _range_span(sym.name_range))


def find_identifier_symbol_at_position(
    model: SemanticModel,
    text: str,
    *,
    line: int,
    character: int,
) -> Optional[Symbol]:
    identifier = identifier_at_position(text, line, character)
    if identifier is None:
        return None
    scope = scope_at_line(model.unit_scope, line + 1)
    if scope is None:
        return None
    resolved = scope.resolve(identifier)
    if resolved:
        return resolved[0]
    indexed = model.index.lookup(identifier)
    return indexed[0] if indexed else None


def identifier_at_position(text: str, line: int, character: int) -> Optional[str]:
    lines = text.splitlines()
    if line < 0 or line >= len(lines):
        return None
    line_text = lines[line]
    if not line_text:
        return None
    idx = min(max(character, 0), len(line_text) - 1)
    if not _is_identifier_char(line_text[idx]) and idx > 0 and _is_identifier_char(line_text[idx - 1]):
        idx -= 1
    if not _is_identifier_char(line_text[idx]):
        return None
    start = idx
    while start > 0 and _is_identifier_char(line_text[start - 1]):
        start -= 1
    end = idx + 1
    while end < len(line_text) and _is_identifier_char(line_text[end]):
        end += 1
    return line_text[start:end]


def scope_at_line(scope: Scope, line_1: int) -> Optional[Scope]:
    best_scope = scope
    best_span = float('inf')
    for symbol in iter_symbols(scope):
        member_scope = symbol.member_scope
        if member_scope is None:
            continue
        if not (symbol.decl_range.start_line <= line_1 <= symbol.decl_range.end_line):
            continue
        span = symbol.decl_range.end_line - symbol.decl_range.start_line
        if span < best_span:
            best_span = span
            best_scope = member_scope
    return best_scope


def _is_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch == '_'


def _range_span(range_value: SourceRange) -> tuple[int, int]:
    return (
        range_value.end_line - range_value.start_line,
        range_value.end_col - range_value.start_col,
    )


def _reference_span(reference: SymbolReference) -> tuple[int, int]:
    return _range_span(reference.ref_range)


def iter_symbols(scope) -> Iterable[Symbol]:
    for symbols in scope.symbols.values():
        for symbol in symbols:
            yield symbol
            if symbol.member_scope is not None:
                yield from iter_symbols(symbol.member_scope)


def hover_text(symbol: Symbol) -> str:
    type_desc = symbol.type_ref.display_name()
    return f'{symbol.kind.value} {symbol.name}: {type_desc}'


def source_range_to_lsp(range_value: SourceRange) -> tuple[int, int, int, int]:
    return (
        range_value.start_line - 1,
        range_value.start_col - 1,
        range_value.end_line - 1,
        range_value.end_col - 1,
    )


def resolve_reference(model: SemanticModel, name: str, scope: Scope | None = None) -> Optional[Symbol]:
    builder = SemanticBuilder()
    builder._index = model.index
    return builder._resolve_reference(name, ReferenceKind.VALUE, scope or model.unit_scope)


def split_reference_parts(name: str) -> list[str]:
    builder = SemanticBuilder()
    return builder._normalized_reference_parts(name)


def resolve_base_for_member_completion(model: SemanticModel, name: str, scope: Scope | None = None) -> Optional[Symbol]:
    lookup_scope = scope or model.unit_scope
    parts = split_reference_parts(name)
    if len(parts) <= 1:
        symbol = resolve_reference(model, name, lookup_scope)
        if symbol is not None:
            return symbol
        symbols = model.index.lookup(name)
        return symbols[0] if symbols else None
    base_name = '.'.join(parts[:-1])
    symbol = resolve_reference(model, base_name, lookup_scope)
    if symbol is not None:
        return symbol
    symbols = model.index.lookup(base_name)
    return symbols[0] if symbols else None


def iter_member_symbols(model: SemanticModel, symbol: Symbol) -> Iterable[Symbol]:
    builder = SemanticBuilder()
    builder._index = model.index
    for scope in builder._iter_member_scopes(symbol, model.unit_scope):
        for symbols in scope.symbols.values():
            for member in symbols:
                yield member


def completion_items_for_scope(scope) -> list[Symbol]:
    seen: set[str] = set()
    items: list[Symbol] = []
    for symbols in scope.symbols.values():
        for symbol in symbols:
            key = symbol.name.casefold()
            if key not in seen:
                seen.add(key)
                items.append(symbol)
    for imported in scope.imports:
        for symbols in imported.symbols.values():
            for symbol in symbols:
                key = symbol.name.casefold()
                if key not in seen:
                    seen.add(key)
                    items.append(symbol)
    return items


def references_for_symbol(workspace: WorkspaceSemanticResult, symbol: Symbol) -> list[SymbolReference]:
    refs: list[SymbolReference] = []
    for model in workspace.models.values():
        for ref in model.references:
            if ref.resolved is symbol:
                refs.append(ref)
    return refs


def text_references_for_symbol(
    text: str,
    symbol: Symbol,
    *,
    include_declaration: bool,
) -> list[SourceRange]:
    search_range = _text_reference_search_range(symbol)
    lines = text.splitlines()
    if search_range.start_line < 1 or search_range.start_line > len(lines):
        return []
    end_line = min(search_range.end_line, len(lines))
    pattern = re.compile(rf'\b{re.escape(symbol.name)}\b', re.IGNORECASE)
    ranges: list[SourceRange] = []
    for line_number in range(search_range.start_line, end_line + 1):
        line_text = lines[line_number - 1]
        for match in pattern.finditer(line_text):
            ref_range = SourceRange(
                symbol.decl_range.file_name,
                line_number,
                match.start() + 1,
                line_number,
                match.end() + 1,
            )
            if not include_declaration and _same_range(ref_range, symbol.name_range):
                continue
            ranges.append(ref_range)
    return ranges


def _text_reference_search_range(symbol: Symbol) -> SourceRange:
    owner = symbol.scope.owner
    if owner is not None and owner is not symbol:
        return owner.decl_range
    return SourceRange(
        symbol.decl_range.file_name,
        1,
        1,
        max(symbol.decl_range.end_line, 1_000_000_000),
        1,
    )


def _same_range(left: SourceRange, right: SourceRange) -> bool:
    return (
        left.file_name == right.file_name
        and left.start_line == right.start_line
        and left.start_col == right.start_col
        and left.end_line == right.end_line
        and left.end_col == right.end_col
    )


def diagnostics_for_model(model: SemanticModel):
    try:
        from lsprotocol.types import Diagnostic, DiagnosticSeverity, Range, Position
    except ImportError:
        return []
    diagnostics = []
    for problem in model.problems:
        start_line, start_col, end_line, end_col = source_range_to_lsp(problem.range)
        diagnostics.append(
            Diagnostic(
                range=Range(
                    start=Position(line=start_line, character=start_col),
                    end=Position(line=end_line, character=end_col),
                ),
                message=problem.message,
                severity=DiagnosticSeverity.Error,
                source='delphiast',
            )
        )
    return diagnostics


def extract_completion_base(text: str, line: int, character: int) -> Optional[str]:
    lines = text.splitlines()
    if line < 0 or line >= len(lines):
        return None
    line_text = lines[line]
    if character <= 0 or character > len(line_text):
        return None
    if line_text[character - 1] != '.':
        return None
    idx = character - 2
    if idx < 0:
        return None
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_<>.,&')
    buf = []
    while idx >= 0:
        ch = line_text[idx]
        if ch.isspace():
            break
        if ch in allowed:
            buf.append(ch)
            idx -= 1
            continue
        break
    if not buf:
        return None
    return ''.join(reversed(buf)).strip()


# LSP server entrypoint

def create_server():
    try:
        from pygls.server import LanguageServer
        from lsprotocol.types import (
            CompletionItem,
            CompletionItemKind,
            CompletionList,
            CompletionOptions,
            CompletionParams,
            DefinitionParams,
            Hover,
            HoverParams,
            InitializeParams,
            InitializeResult,
            Location,
            Position,
            Range,
            ReferenceParams,
            RenameParams,
            ServerCapabilities,
            SymbolInformation,
            SymbolKind as LspSymbolKind,
            TextDocumentSyncKind,
            TextDocumentSyncOptions,
            TextEdit,
            WorkspaceEdit,
            WorkspaceSymbolParams,
            DocumentSymbolParams,
            DocumentSymbol,
            TEXT_DOCUMENT_COMPLETION,
            TEXT_DOCUMENT_DEFINITION,
            TEXT_DOCUMENT_DID_CHANGE,
            TEXT_DOCUMENT_DID_CLOSE,
            TEXT_DOCUMENT_DID_OPEN,
            TEXT_DOCUMENT_HOVER,
            TEXT_DOCUMENT_REFERENCES,
            TEXT_DOCUMENT_RENAME,
            TEXT_DOCUMENT_DOCUMENT_SYMBOL,
            WORKSPACE_SYMBOL,
            INITIALIZE,
        )
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError('pygls and lsprotocol are required for the LSP server') from exc

    server = LanguageServer('delphiast', '0.1.0')
    state = LspWorkspaceState()

    def _symbol_kind(symbol: Symbol) -> LspSymbolKind:
        mapping = {
            SymbolKind.CLASS: LspSymbolKind.Class,
            SymbolKind.RECORD: LspSymbolKind.Struct,
            SymbolKind.INTERFACE: LspSymbolKind.Interface,
            SymbolKind.ENUM: LspSymbolKind.Enum,
            SymbolKind.ENUM_VALUE: LspSymbolKind.EnumMember,
            SymbolKind.FIELD: LspSymbolKind.Field,
            SymbolKind.PROPERTY: LspSymbolKind.Property,
            SymbolKind.METHOD: LspSymbolKind.Method,
            SymbolKind.FUNCTION: LspSymbolKind.Function,
            SymbolKind.PROCEDURE: LspSymbolKind.Function,
            SymbolKind.CONSTRUCTOR: LspSymbolKind.Constructor,
            SymbolKind.DESTRUCTOR: LspSymbolKind.Method,
            SymbolKind.VARIABLE: LspSymbolKind.Variable,
            SymbolKind.PARAMETER: LspSymbolKind.Variable,
            SymbolKind.CONSTANT: LspSymbolKind.Constant,
            SymbolKind.TYPE: LspSymbolKind.Class,
        }
        return mapping.get(symbol.kind, LspSymbolKind.String)

    def _completion_kind(symbol: Symbol) -> CompletionItemKind:
        mapping = {
            SymbolKind.CLASS: CompletionItemKind.Class,
            SymbolKind.RECORD: CompletionItemKind.Struct,
            SymbolKind.INTERFACE: CompletionItemKind.Interface,
            SymbolKind.ENUM: CompletionItemKind.Enum,
            SymbolKind.ENUM_VALUE: CompletionItemKind.EnumMember,
            SymbolKind.FIELD: CompletionItemKind.Field,
            SymbolKind.PROPERTY: CompletionItemKind.Property,
            SymbolKind.METHOD: CompletionItemKind.Method,
            SymbolKind.FUNCTION: CompletionItemKind.Function,
            SymbolKind.PROCEDURE: CompletionItemKind.Function,
            SymbolKind.CONSTRUCTOR: CompletionItemKind.Constructor,
            SymbolKind.DESTRUCTOR: CompletionItemKind.Method,
            SymbolKind.VARIABLE: CompletionItemKind.Variable,
            SymbolKind.PARAMETER: CompletionItemKind.Variable,
            SymbolKind.CONSTANT: CompletionItemKind.Constant,
            SymbolKind.TYPE: CompletionItemKind.Class,
        }
        return mapping.get(symbol.kind, CompletionItemKind.Text)

    def _document_symbols_for_scope(scope) -> list[DocumentSymbol]:
        items: list[DocumentSymbol] = []
        for symbols in scope.symbols.values():
            for symbol in symbols:
                if symbol.kind == SymbolKind.UNIT:
                    continue
                start_line, start_col, end_line, end_col = source_range_to_lsp(symbol.decl_range)
                sel_start, sel_col, sel_end, sel_end_col = source_range_to_lsp(symbol.name_range)
                children = []
                if symbol.member_scope is not None:
                    children = _document_symbols_for_scope(symbol.member_scope)
                items.append(
                    DocumentSymbol(
                        name=symbol.name,
                        kind=_symbol_kind(symbol),
                        range=Range(
                            start=Position(line=start_line, character=start_col),
                            end=Position(line=end_line, character=end_col),
                        ),
                        selection_range=Range(
                            start=Position(line=sel_start, character=sel_col),
                            end=Position(line=sel_end, character=sel_end_col),
                        ),
                        children=children or None,
                    )
                )
        return items

    @server.feature(INITIALIZE)
    def initialize(ls: LanguageServer, params: InitializeParams) -> InitializeResult:
        roots: list[str] = []
        if params.workspace_folders:
            roots.extend(uri_to_path(folder.uri) for folder in params.workspace_folders)
        elif params.root_uri:
            roots.append(uri_to_path(params.root_uri))
        init_opts = params.initialization_options or {}
        include_paths = [uri_to_path(path) for path in init_opts.get('includePaths', [])]
        search_paths = [uri_to_path(path) for path in init_opts.get('searchPaths', [])]
        defines = init_opts.get('defines', [])
        auto_discover_paths = init_opts.get('autoDiscoverPaths', True)
        config = WorkspaceConfig(
            roots=roots,
            include_paths=include_paths,
            search_paths=search_paths,
            defines=defines,
            auto_discover_paths=bool(auto_discover_paths),
        )
        state.configure(config)

        capabilities = ServerCapabilities(
            text_document_sync=TextDocumentSyncOptions(
                open_close=True,
                change=TextDocumentSyncKind.Full,
            ),
            definition_provider=True,
            references_provider=True,
            hover_provider=True,
            completion_provider=CompletionOptions(trigger_characters=['.']),
            rename_provider=True,
            document_symbol_provider=True,
            workspace_symbol_provider=True,
        )
        return InitializeResult(capabilities=capabilities)

    def _publish_diagnostics(ls: LanguageServer, uri: str) -> None:
        diagnostics = state.diagnostics_for_uri(uri)
        ls.publish_diagnostics(uri, diagnostics)

    def _symbol_at_position_in_model(
        model: SemanticModel,
        uri: str,
        position: Position,
        text: str | None = None,
    ) -> Optional[Symbol]:
        if text is None:
            text = state.text_for_uri(uri)
        ref = find_reference_at_position(
            model,
            line=position.line,
            character=position.character,
        )
        symbol = ref.resolved if ref and ref.resolved else find_symbol_at_position(
            model,
            line=position.line,
            character=position.character,
        )
        if symbol is None and text is not None:
            symbol = find_identifier_symbol_at_position(
                model,
                text,
                line=position.line,
                character=position.character,
            )
        return symbol

    def _symbol_at_position(uri: str, position: Position) -> Optional[Symbol]:
        text = state.text_for_uri(uri)
        seen: set[int] = set()
        model = state.semantic_for_uri(uri)
        if model is not None:
            seen.add(id(model))
            symbol = _symbol_at_position_in_model(model, uri, position, text)
            if symbol is not None:
                return symbol
        model = state.structure_semantic_for_uri(uri)
        if model is not None:
            if id(model) in seen:
                return None
            seen.add(id(model))
            symbol = _symbol_at_position_in_model(model, uri, position, text)
            if symbol is not None:
                return symbol
        return None

    def _add_rename_edit(
        edits: dict[str, list[TextEdit]],
        seen: set[tuple[str, int, int, int, int]],
        ref_range: SourceRange,
        new_text: str,
    ) -> None:
        uri = state.uri_for_file_name(ref_range.file_name) or path_to_uri(ref_range.file_name)
        key = (uri, ref_range.start_line, ref_range.start_col, ref_range.end_line, ref_range.end_col)
        if key in seen:
            return
        seen.add(key)
        start_line, start_col, end_line, end_col = source_range_to_lsp(ref_range)
        edits.setdefault(uri, []).append(
            TextEdit(
                range=Range(
                    start=Position(line=start_line, character=start_col),
                    end=Position(line=end_line, character=end_col),
                ),
                new_text=new_text,
            )
        )

    @server.feature(TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls: LanguageServer, params) -> None:
        state.update_document(params.text_document.uri, params.text_document.text)
        _publish_diagnostics(ls, params.text_document.uri)

    @server.feature(TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls: LanguageServer, params) -> None:
        if not params.content_changes:
            return
        text = params.content_changes[-1].text
        state.update_document(params.text_document.uri, text)
        _publish_diagnostics(ls, params.text_document.uri)

    @server.feature(TEXT_DOCUMENT_DID_CLOSE)
    def did_close(ls: LanguageServer, params) -> None:
        state.remove_document(params.text_document.uri)
        ls.publish_diagnostics(params.text_document.uri, [])

    @server.feature(TEXT_DOCUMENT_DEFINITION)
    def definition(ls: LanguageServer, params: DefinitionParams):
        symbol = _symbol_at_position(params.text_document.uri, params.position)
        if symbol is None:
            return None
        file_name = symbol.decl_range.file_name
        uri = state.uri_for_file_name(file_name) or path_to_uri(file_name)
        start_line, start_col, end_line, end_col = source_range_to_lsp(symbol.decl_range)
        return Location(
            uri=uri,
            range=Range(
                start=Position(line=start_line, character=start_col),
                end=Position(line=end_line, character=end_col),
            ),
        )

    @server.feature(TEXT_DOCUMENT_HOVER)
    def hover(ls: LanguageServer, params: HoverParams) -> Optional[Hover]:
        symbol = _symbol_at_position(params.text_document.uri, params.position)
        if symbol is None:
            return None
        return Hover(contents=hover_text(symbol))

    @server.feature(TEXT_DOCUMENT_REFERENCES)
    def references(ls: LanguageServer, params: ReferenceParams):
        symbol = _symbol_at_position(params.text_document.uri, params.position)
        if symbol is None:
            return []
        locations = []
        if state.workspace is not None:
            for ref in references_for_symbol(state.workspace, symbol):
                file_name = ref.ref_range.file_name
                uri = state.uri_for_file_name(file_name) or path_to_uri(file_name)
                start_line, start_col, end_line, end_col = source_range_to_lsp(ref.ref_range)
                locations.append(
                    Location(
                        uri=uri,
                        range=Range(
                            start=Position(line=start_line, character=start_col),
                            end=Position(line=end_line, character=end_col),
                        ),
                    )
                )
        if locations:
            return locations
        text = state.text_for_uri(params.text_document.uri)
        if text is None:
            return []
        include_declaration = getattr(params.context, 'include_declaration', True)
        for ref_range in text_references_for_symbol(
            text,
            symbol,
            include_declaration=include_declaration,
        ):
            uri = state.uri_for_file_name(ref_range.file_name) or path_to_uri(ref_range.file_name)
            start_line, start_col, end_line, end_col = source_range_to_lsp(ref_range)
            locations.append(
                Location(
                    uri=uri,
                    range=Range(
                        start=Position(line=start_line, character=start_col),
                        end=Position(line=end_line, character=end_col),
                    ),
                )
            )
        return locations

    @server.feature(TEXT_DOCUMENT_RENAME)
    def rename(ls: LanguageServer, params: RenameParams) -> Optional[WorkspaceEdit]:
        symbol = _symbol_at_position(params.text_document.uri, params.position)
        if symbol is None:
            return None

        text = state.text_for_uri(params.text_document.uri)
        semantic_symbol: Symbol | None = None
        semantic_model = state.semantic_for_uri(params.text_document.uri)
        if semantic_model is not None:
            semantic_symbol = _symbol_at_position_in_model(
                semantic_model,
                params.text_document.uri,
                params.position,
                text,
            )

        edits: dict[str, list[TextEdit]] = {}
        seen: set[tuple[str, int, int, int, int]] = set()
        workspace_symbol = semantic_symbol or symbol
        if state.workspace is not None and semantic_symbol is not None:
            for ref in references_for_symbol(state.workspace, semantic_symbol):
                if '.' in ref.name:
                    continue
                _add_rename_edit(edits, seen, ref.ref_range, params.new_name)

        if not edits and text is not None:
            for ref_range in text_references_for_symbol(
                text,
                symbol,
                include_declaration=True,
            ):
                _add_rename_edit(edits, seen, ref_range, params.new_name)

        _add_rename_edit(
            edits,
            seen,
            workspace_symbol.name_range,
            params.new_name,
        )
        return WorkspaceEdit(changes=edits)

    @server.feature(TEXT_DOCUMENT_DOCUMENT_SYMBOL)
    def document_symbols(ls: LanguageServer, params: DocumentSymbolParams):
        model = state.structure_semantic_for_uri(params.text_document.uri)
        if model is None:
            return []
        return _document_symbols_for_scope(model.unit_scope)

    @server.feature(WORKSPACE_SYMBOL)
    def workspace_symbols(ls: LanguageServer, params: WorkspaceSymbolParams):
        query = (params.query or '').strip().casefold()
        workspace = state.workspace_symbols_for_query(query)
        if workspace is None:
            return []
        items: list[SymbolInformation] = []
        for model in workspace.models.values():
            for symbol in iter_symbols(model.unit_scope):
                if symbol.kind == SymbolKind.UNIT:
                    continue
                if query and query not in symbol.name.casefold():
                    continue
                file_name = symbol.decl_range.file_name
                uri = state.uri_for_file_name(file_name) or path_to_uri(file_name)
                start_line, start_col, end_line, end_col = source_range_to_lsp(symbol.decl_range)
                items.append(
                    SymbolInformation(
                        name=symbol.name,
                        kind=_symbol_kind(symbol),
                        location=Location(
                            uri=uri,
                            range=Range(
                                start=Position(line=start_line, character=start_col),
                                end=Position(line=end_line, character=end_col),
                            ),
                        ),
                    )
                )
        return items

    @server.feature(TEXT_DOCUMENT_COMPLETION)
    def completion(ls: LanguageServer, params: CompletionParams):
        model = state.structure_semantic_for_uri(params.text_document.uri)
        if model is None:
            return CompletionList(is_incomplete=True, items=[])
        doc = state.documents.get(params.text_document.uri)
        base_expr = None
        if doc is not None:
            base_expr = extract_completion_base(doc.text, params.position.line, params.position.character)
        active_scope = scope_at_line(model.unit_scope, params.position.line + 1) or model.unit_scope
        symbols: list[Symbol]
        if base_expr:
            base_symbol = resolve_reference(model, base_expr, active_scope)
            if base_symbol is None:
                base_symbol = resolve_base_for_member_completion(model, base_expr, active_scope)
            if base_symbol is not None:
                symbols = list(iter_member_symbols(model, base_symbol))
            else:
                symbols = completion_items_for_scope(active_scope)
            if not symbols:
                full_model = state.semantic_for_uri(params.text_document.uri)
                if full_model is not None and full_model is not model:
                    full_scope = scope_at_line(full_model.unit_scope, params.position.line + 1) or full_model.unit_scope
                    full_base_symbol = resolve_reference(full_model, base_expr, full_scope)
                    if full_base_symbol is None:
                        full_base_symbol = resolve_base_for_member_completion(full_model, base_expr, full_scope)
                    if full_base_symbol is not None:
                        symbols = list(iter_member_symbols(full_model, full_base_symbol))
        else:
            full_model = state.semantic_for_uri(params.text_document.uri)
            if full_model is not None:
                full_scope = scope_at_line(full_model.unit_scope, params.position.line + 1) or full_model.unit_scope
                symbols = completion_items_for_scope(full_scope)
            else:
                symbols = completion_items_for_scope(active_scope)
        items = [CompletionItem(label=symbol.name, kind=_completion_kind(symbol)) for symbol in symbols]
        return CompletionList(is_incomplete=False, items=items)

    return server


def main() -> None:
    server = create_server()
    server.start_io()


__all__ = [
    'create_server',
    'LspWorkspaceState',
    'WorkspaceConfig',
    'DocumentSnapshot',
    'find_reference_at_position',
    'find_symbol_at_position',
    'resolve_reference',
    'extract_completion_base',
    'outline_source',
    'outline_large_source',
    'multiline_string_block_end',
    'main',
]


if __name__ == '__main__':
    main()
