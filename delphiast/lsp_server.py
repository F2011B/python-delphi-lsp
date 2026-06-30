from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse
import os

from .semantic import ReferenceKind, SourceRange, Symbol, SymbolKind, SymbolReference, TypeRef, NamedTypeRef, GenericInstanceTypeRef
from .semantic_builder import SemanticBuilder, SemanticModel
from .workspace import WorkspaceSemanticResult, build_workspace_semantics


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
    defines: list[str] = field(default_factory=list)
    extensions: tuple[str, ...] = ('.pas', '.dpr', '.dpk')


@dataclass
class LspWorkspaceState:
    config: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    documents: dict[str, DocumentSnapshot] = field(default_factory=dict)
    workspace: Optional[WorkspaceSemanticResult] = None
    workspace_files: set[str] = field(default_factory=set)
    file_cache: dict[str, FileSnapshot] = field(default_factory=dict)

    def configure(self, config: WorkspaceConfig) -> None:
        self.config = config
        self.index_workspace()

    def index_workspace(self) -> None:
        self.workspace_files = set(self._scan_workspace_files())
        self._refresh_file_cache()
        self._rebuild()

    def update_document(self, uri: str, text: str) -> None:
        file_name = uri_to_path(uri)
        self.documents[uri] = DocumentSnapshot(uri=uri, file_name=file_name, text=text)
        self._rebuild()

    def remove_document(self, uri: str) -> None:
        if uri in self.documents:
            self.documents.pop(uri)
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
        sources = {path: snapshot.text for path, snapshot in self.file_cache.items()}
        for doc in self.documents.values():
            sources[doc.file_name] = doc.text
        return sources

    def _rebuild(self) -> None:
        sources = self._collect_sources()
        if not sources:
            self.workspace = None
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
        return doc.semantic if doc else None

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


def path_to_uri(path: str) -> str:
    if '://' in path:
        return path
    return Path(path).absolute().as_uri()


def read_source_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return data.decode('utf-16')
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('latin-1')


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


def resolve_reference(model: SemanticModel, name: str) -> Optional[Symbol]:
    builder = SemanticBuilder()
    builder._index = model.index
    return builder._resolve_reference(name, ReferenceKind.VALUE, model.unit_scope)


def split_reference_parts(name: str) -> list[str]:
    builder = SemanticBuilder()
    return builder._normalized_reference_parts(name)


def resolve_base_for_member_completion(model: SemanticModel, name: str) -> Optional[Symbol]:
    parts = split_reference_parts(name)
    if len(parts) <= 1:
        symbol = resolve_reference(model, name)
        if symbol is not None:
            return symbol
        symbols = model.index.lookup(name)
        return symbols[0] if symbols else None
    base_name = '.'.join(parts[:-1])
    symbol = resolve_reference(model, base_name)
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
            SymbolKind.CLASS: CompletionItemKind.CLASS,
            SymbolKind.RECORD: CompletionItemKind.STRUCT,
            SymbolKind.INTERFACE: CompletionItemKind.INTERFACE,
            SymbolKind.ENUM: CompletionItemKind.ENUM,
            SymbolKind.ENUM_VALUE: CompletionItemKind.ENUM_MEMBER,
            SymbolKind.FIELD: CompletionItemKind.FIELD,
            SymbolKind.PROPERTY: CompletionItemKind.PROPERTY,
            SymbolKind.METHOD: CompletionItemKind.METHOD,
            SymbolKind.FUNCTION: CompletionItemKind.FUNCTION,
            SymbolKind.PROCEDURE: CompletionItemKind.FUNCTION,
            SymbolKind.CONSTRUCTOR: CompletionItemKind.CONSTRUCTOR,
            SymbolKind.DESTRUCTOR: CompletionItemKind.METHOD,
            SymbolKind.VARIABLE: CompletionItemKind.VARIABLE,
            SymbolKind.PARAMETER: CompletionItemKind.VARIABLE,
            SymbolKind.CONSTANT: CompletionItemKind.CONSTANT,
            SymbolKind.TYPE: CompletionItemKind.CLASS,
        }
        return mapping.get(symbol.kind, CompletionItemKind.TEXT)

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
    def initialize(_: LanguageServer, params: InitializeParams) -> InitializeResult:
        roots: list[str] = []
        if params.workspace_folders:
            roots.extend(uri_to_path(folder.uri) for folder in params.workspace_folders)
        elif params.root_uri:
            roots.append(uri_to_path(params.root_uri))
        init_opts = params.initialization_options or {}
        include_paths = [uri_to_path(path) for path in init_opts.get('includePaths', [])]
        defines = init_opts.get('defines', [])
        config = WorkspaceConfig(roots=roots, include_paths=include_paths, defines=defines)
        state.configure(config)

        capabilities = ServerCapabilities(
            text_document_sync=TextDocumentSyncOptions(
                open_close=True,
                change=TextDocumentSyncKind.FULL,
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
    def definition(_: LanguageServer, params: DefinitionParams):
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None:
            return None
        ref = find_reference_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        symbol = ref.resolved if ref and ref.resolved else find_symbol_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
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
    def hover(_: LanguageServer, params: HoverParams) -> Optional[Hover]:
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None:
            return None
        ref = find_reference_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        symbol = ref.resolved if ref and ref.resolved else find_symbol_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        if symbol is None:
            return None
        return Hover(contents=hover_text(symbol))

    @server.feature(TEXT_DOCUMENT_REFERENCES)
    def references(_: LanguageServer, params: ReferenceParams):
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None or state.workspace is None:
            return []
        ref = find_reference_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        symbol = ref.resolved if ref and ref.resolved else find_symbol_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        if symbol is None:
            return []
        locations = []
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
        return locations

    @server.feature(TEXT_DOCUMENT_RENAME)
    def rename(_: LanguageServer, params: RenameParams) -> Optional[WorkspaceEdit]:
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None or state.workspace is None:
            return None
        ref = find_reference_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        symbol = ref.resolved if ref and ref.resolved else find_symbol_at_position(
            model,
            line=params.position.line,
            character=params.position.character,
        )
        if symbol is None:
            return None
        edits: dict[str, list[TextEdit]] = {}
        for ref in references_for_symbol(state.workspace, symbol):
            if '.' in ref.name:
                continue
            file_name = ref.ref_range.file_name
            uri = state.uri_for_file_name(file_name) or path_to_uri(file_name)
            start_line, start_col, end_line, end_col = source_range_to_lsp(ref.ref_range)
            edits.setdefault(uri, []).append(
                TextEdit(
                    range=Range(
                        start=Position(line=start_line, character=start_col),
                        end=Position(line=end_line, character=end_col),
                    ),
                    new_text=params.new_name,
                )
            )
        start_line, start_col, end_line, end_col = source_range_to_lsp(symbol.name_range)
        uri = state.uri_for_file_name(symbol.name_range.file_name) or path_to_uri(symbol.name_range.file_name)
        edits.setdefault(uri, []).append(
            TextEdit(
                range=Range(
                    start=Position(line=start_line, character=start_col),
                    end=Position(line=end_line, character=end_col),
                ),
                new_text=params.new_name,
            )
        )
        return WorkspaceEdit(changes=edits)

    @server.feature(TEXT_DOCUMENT_DOCUMENT_SYMBOL)
    def document_symbols(_: LanguageServer, params: DocumentSymbolParams):
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None:
            return []
        return _document_symbols_for_scope(model.unit_scope)

    @server.feature(WORKSPACE_SYMBOL)
    def workspace_symbols(_: LanguageServer, params: WorkspaceSymbolParams):
        if state.workspace is None:
            return []
        query = (params.query or '').casefold()
        items: list[SymbolInformation] = []
        for model in state.workspace.models.values():
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
    def completion(_: LanguageServer, params: CompletionParams):
        model = state.semantic_for_uri(params.text_document.uri)
        if model is None:
            return CompletionList(is_incomplete=True, items=[])
        doc = state.documents.get(params.text_document.uri)
        base_expr = None
        if doc is not None:
            base_expr = extract_completion_base(doc.text, params.position.line, params.position.character)
        symbols: list[Symbol]
        if base_expr:
            base_symbol = resolve_reference(model, base_expr)
            if base_symbol is None:
                base_symbol = resolve_base_for_member_completion(model, base_expr)
            if base_symbol is not None:
                symbols = list(iter_member_symbols(model, base_symbol))
            else:
                symbols = completion_items_for_scope(model.unit_scope)
        else:
            symbols = completion_items_for_scope(model.unit_scope)
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
    'main',
]


if __name__ == '__main__':
    main()
