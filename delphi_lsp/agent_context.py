from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from heapq import heappop, heappush
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys
import time
import unicodedata

from .agent_protocol import (
    AgentProtocolError,
    AgentRequest,
    AgentResponse,
    ContextBudget,
    Focus,
    make_target_id,
    paginate_items,
)
from .agent_cpg import CpgSubgraph, CpgTarget
from .agent_cpg_builder import build_cpg_subgraph
from .agent_metrics import build_workspace_metrics, project_metric_item, unit_metric_item
from .agent_relations import ProjectRelationIndex, RelationTarget
from .agent_workspace import AgentUnit, AgentWorkspace, unit_display_path, unit_source_path, unit_target_id
from .consts import AttributeName, SyntaxNodeType
from .lsp_server import build_outline_semantic_model, multiline_string_block_end
from .metrics import ProjectMetrics
from .navigation_cache import NavigationShardStore, navigation_cache_key
from .nodes import CompoundSyntaxNode, SyntaxNode
from .parser import DelphiParser
from .parser_backend import ParserMode
from .project_config import ProjectPathConfig, workspace_include_loader
from .semantic import (
    Scope,
    ScopeKind,
    Symbol,
    SymbolKind,
    Visibility,
)
from .parallel_outline import (
    ParallelBuildStats,
    ParallelOutlineError,
    run_outline_tasks,
)
from .source_reader import read_source_text


_ROUTINE_KINDS = frozenset(
    {
        SymbolKind.METHOD,
        SymbolKind.FUNCTION,
        SymbolKind.PROCEDURE,
        SymbolKind.CONSTRUCTOR,
        SymbolKind.DESTRUCTOR,
    }
)
_TYPE_KINDS = frozenset(
    {
        SymbolKind.TYPE,
        SymbolKind.CLASS,
        SymbolKind.RECORD,
        SymbolKind.INTERFACE,
        SymbolKind.ENUM,
    }
)
_ROUTINE_WORDS = frozenset(
    {"procedure", "function", "constructor", "destructor", "operator"}
)
_BLOCK_WORDS = frozenset({"begin", "case", "try", "asm"})
_STRUCTURED_TYPE_WORDS = frozenset(
    {"class", "record", "object", "interface", "dispinterface"}
)
_NO_BODY_DIRECTIVES = frozenset({"abstract", "external", "forward"})
_ROUTINE_DIRECTIVES = frozenset(
    {
        "abstract",
        "assembler",
        "cdecl",
        "deprecated",
        "dispid",
        "dynamic",
        "experimental",
        "export",
        "external",
        "final",
        "forward",
        "inline",
        "message",
        "overload",
        "override",
        "pascal",
        "platform",
        "register",
        "reintroduce",
        "safecall",
        "static",
        "stdcall",
        "unsafe",
        "varargs",
        "virtual",
        "winapi",
    }
)
_CALLING_CONVENTIONS = frozenset(
    {"cdecl", "pascal", "register", "safecall", "stdcall", "winapi"}
)
_SOURCE_CHUNK_CHARS = 6000
_RANKED_QUERY_CACHE_SIZE = 16
_RANKED_QUERY_CACHE_MAX_ENTRIES = 50_000
_PREPARED_RESPONSE_CACHE_SIZE = 8
_DEFAULT_NAVIGATION_CACHE_MAX_BYTES = 512 * 1024**2


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    start: int
    end: int
    word: bool = False
    escaped: bool = False
    directive: bool = False


class _SourceDocument:
    def __init__(
        self,
        source_path: Path,
        display_path: str,
        text: str,
        *,
        defines: tuple[str, ...] = (),
        include_paths: tuple[str, ...] = (),
        project_config: ProjectPathConfig | None = None,
    ) -> None:
        self.source_path = source_path
        self.display_path = display_path
        self.text = text
        self.defines = defines
        self.include_paths = include_paths
        self.project_config = project_config
        self.line_starts = _line_starts(text)
        self.tokens = tuple(_lex_delphi(text))
        self.token_starts = tuple(token.start for token in self.tokens)
        self.directive_starts = tuple(token.start for token in self.tokens if token.directive)
        self.declaration_section_indexes = [0]
        self.declaration_section_checkpoints: dict[int, tuple[str, int, int, int]] = {
            0: ("", 0, 0, 0)
        }
        words = [token for token in self.tokens if token.word and not token.escaped]
        self.unit_kind = next(
            (token.value for token in words if token.value in {"unit", "program", "library", "package"}),
            "",
        )
        implementation = next((token for token in words if token.value == "implementation"), None)
        self.implementation_line = self.line_col(implementation.start)[0] if implementation else 0
        self.routine_spans: dict[int, tuple[int, int] | None] = {}
        self.routine_token_spans: dict[int, tuple[int, int, int] | None] = {}
        self.parser_spans: dict[str, tuple[int, int] | None] = {}
        self._full_parse_attempted = False
        self._full_parse_result: object | None = None
        self.retained_bytes = (
            sys.getsizeof(self)
            + sys.getsizeof(self.text)
            + sys.getsizeof(self.line_starts)
            + len(self.line_starts) * 32
            + sys.getsizeof(self.tokens)
            + len(self.tokens) * 160
            + sys.getsizeof(self.token_starts)
            + len(self.token_starts) * 28
            + sys.getsizeof(self.directive_starts)
            + len(self.directive_starts) * 28
        )

    def offset(self, line: int, column: int = 1) -> int:
        if not self.line_starts:
            return 0
        line_index = min(max(line - 1, 0), len(self.line_starts) - 1)
        start = self.line_starts[line_index]
        end = self.line_starts[line_index + 1] if line_index + 1 < len(self.line_starts) else len(self.text)
        return min(start + max(column - 1, 0), end)

    def line_start(self, line: int) -> int:
        return self.offset(line, 1)

    def line_end(self, line: int, *, include_newline: bool = False) -> int:
        if line < len(self.line_starts):
            end = self.line_starts[max(line, 0)]
        else:
            end = len(self.text)
        if include_newline:
            return end
        while end > 0 and self.text[end - 1] in {"\r", "\n"}:
            end -= 1
        return end

    def line_col(self, offset: int) -> tuple[int, int]:
        clamped = min(max(offset, 0), len(self.text))
        line_index = max(0, bisect_right(self.line_starts, clamped) - 1)
        return line_index + 1, clamped - self.line_starts[line_index] + 1

    def first_token_index(self, offset: int) -> int:
        return bisect_left(self.token_starts, offset)

    def contains_directive(self, start: int, end: int) -> bool:
        index = bisect_left(self.directive_starts, start)
        return index < len(self.directive_starts) and self.directive_starts[index] < end

    def full_parse(self):
        if not self._full_parse_attempted:
            self._full_parse_attempted = True
            try:
                self._full_parse_result = DelphiParser(
                    defines=self.defines,
                    include_paths=self.include_paths,
                    include_loader=workspace_include_loader(
                        self.project_config,
                        self.include_paths,
                    ),
                ).parse(self.text, str(self.source_path), build_semantic=False)
            except Exception:
                self._full_parse_result = None
        return self._full_parse_result


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    source_path: Path
    display_path: str
    defines: tuple[str, ...]
    include_paths: tuple[str, ...]
    project_config: ProjectPathConfig | None = None


class _SourceStore:
    """Load expensive tokenized source documents only when source evidence is requested."""

    def __init__(
        self,
        specs: Mapping[Path, _SourceSpec],
        *,
        max_loaded_bytes: int,
    ) -> None:
        self._specs = dict(specs)
        self._loaded: OrderedDict[Path, _SourceDocument] = OrderedDict()
        self._loaded_bytes = 0
        self._max_loaded_bytes = max(0, max_loaded_bytes)

    def __getitem__(self, source_path: Path) -> _SourceDocument:
        cached = self._loaded.pop(source_path, None)
        if cached is not None:
            self._loaded[source_path] = cached
            return cached

        spec = self._specs[source_path]
        document = _SourceDocument(
            spec.source_path,
            spec.display_path,
            read_source_text(spec.source_path),
            defines=spec.defines,
            include_paths=spec.include_paths,
            project_config=spec.project_config,
        )
        if document.retained_bytes > self._max_loaded_bytes:
            return document
        while (
            self._loaded
            and self._loaded_bytes + document.retained_bytes > self._max_loaded_bytes
        ):
            _, evicted = self._loaded.popitem(last=False)
            self._loaded_bytes -= evicted.retained_bytes
        self._loaded[source_path] = document
        self._loaded_bytes += document.retained_bytes
        return document

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    @property
    def retained_bytes(self) -> int:
        return self._loaded_bytes

    @property
    def metadata_bytes(self) -> int:
        return 512 + sum(
            256
            + sys.getsizeof(path)
            + sys.getsizeof(spec)
            + sys.getsizeof(spec.display_path)
            for path, spec in self._specs.items()
        )

    def clear_loaded(self) -> None:
        self._loaded.clear()
        self._loaded_bytes = 0


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    name: str
    kind: SymbolKind
    line: int
    column: int
    visibility: Visibility
    type_name: str
    source_path: Path
    path: str
    unit_id: str
    unit_name: str
    qualified_name: str
    owner: str
    parent_qualified_name: str
    signature: str


@dataclass(frozen=True, slots=True)
class _NavigationTask:
    ordinal: int
    source_path: str
    display_path: str
    unit_name: str
    unit_path: str
    unit_id: str
    unit_has_error: bool
    defines: tuple[str, ...]
    include_paths: tuple[str, ...]
    cache_key: str = ""


@dataclass(frozen=True, slots=True)
class _NavigationResult:
    ordinal: int
    source_path: str
    text: str
    model: None
    lines_processed: int
    symbols_discovered: int
    read_error: str
    raw_symbols: tuple[_RawSymbol, ...]
    cache_key: str = ""


def _parse_navigation_task(task: _NavigationTask) -> _NavigationResult:
    source_path = Path(task.source_path)
    try:
        text = read_source_text(source_path)
    except (OSError, UnicodeError) as error:
        return _NavigationResult(
            task.ordinal,
            task.source_path,
            "",
            None,
            0,
            0,
            str(error),
            (),
            "",
        )
    try:
        model = build_outline_semantic_model(
            text,
            task.source_path,
            defines=task.defines,
        )
        document = _SourceDocument(
            source_path,
            task.display_path,
            text,
            defines=task.defines,
            include_paths=task.include_paths,
        )
        unit = AgentUnit(
            unit_id=task.unit_id,
            name=task.unit_name,
            path=task.unit_path,
            has_error=task.unit_has_error,
        )
        symbols = _collect_raw_symbols(model.unit_scope, unit, source_path, document)
        raw_symbols = tuple(_exclude_routine_locals(symbols, document))
    except Exception as error:
        raise ParallelOutlineError(
            f"failed to build navigation shard for {task.source_path}: {error}"
        ) from error
    return _NavigationResult(
        task.ordinal,
        task.source_path,
        "",
        None,
        text.count("\n") + (0 if not text or text.endswith(("\n", "\r")) else 1),
        len(raw_symbols),
        "",
        raw_symbols,
        navigation_cache_key(text, task.defines) if task.cache_key else "",
    )


def _navigation_shard_payload(result: _NavigationResult) -> dict[str, object]:
    unit_name = result.raw_symbols[0].unit_name if result.raw_symbols else ""
    symbols: list[dict[str, object]] = []
    for raw in result.raw_symbols:
        symbols.append(
            {
                "name": raw.name,
                "kind": raw.kind.value,
                "line": raw.line,
                "column": raw.column,
                "visibility": raw.visibility.value,
                "type": raw.type_name,
                "qualified_name": raw.qualified_name,
                "owner": raw.owner,
                "parent_qualified_name": raw.parent_qualified_name,
                "signature": raw.signature,
            }
        )
    return {
        "lines_processed": result.lines_processed,
        "unit_name": unit_name,
        "symbols": symbols,
    }


def _navigation_result_from_shard(
    task: _NavigationTask,
    payload: Mapping[str, object],
) -> _NavigationResult | None:
    try:
        lines_processed = _required_int(payload.get("lines_processed"))
        unit_name = _required_string(payload.get("unit_name"))
        records = payload.get("symbols")
        if not isinstance(records, list):
            return None
        unit_id = make_target_id("unit", task.display_path, task.unit_name)
        source_path = Path(task.source_path)
        raw_symbols: list[_RawSymbol] = []
        for value in records:
            if not isinstance(value, Mapping):
                return None
            raw_symbols.append(
                _RawSymbol(
                    name=_required_string(value.get("name")),
                    kind=SymbolKind(_required_string(value.get("kind"))),
                    line=_required_int(value.get("line")),
                    column=_required_int(value.get("column")),
                    visibility=Visibility(_required_string(value.get("visibility"))),
                    type_name=_required_string(value.get("type")),
                    source_path=source_path,
                    path=task.display_path,
                    unit_id=unit_id,
                    unit_name=unit_name or task.unit_name,
                    qualified_name=_required_string(value.get("qualified_name")),
                    owner=_required_string(value.get("owner")),
                    parent_qualified_name=_required_string(
                        value.get("parent_qualified_name")
                    ),
                    signature=_required_string(value.get("signature")),
                )
            )
    except (TypeError, ValueError):
        return None
    return _NavigationResult(
        ordinal=task.ordinal,
        source_path=task.source_path,
        text="",
        model=None,
        lines_processed=lines_processed,
        symbols_discovered=len(raw_symbols),
        read_error="",
        raw_symbols=tuple(raw_symbols),
    )


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("navigation string is malformed")
    return value


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("navigation integer is malformed")
    return value


@dataclass(frozen=True, slots=True)
class _SymbolEntry:
    name: str
    kind: SymbolKind
    line: int
    column: int
    visibility: Visibility
    type_name: str
    source_path: Path
    path: str
    unit_id: str
    unit_name: str
    qualified_name: str
    normalized_name: str
    normalized_qualified_name: str
    relative_name_offset: int
    owner: str
    signature: str
    ordinal: int
    target_id: str
    card_json_chars: int
    card_json_upper_bound: int
    parent_target_id: str = ""

    def card(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "unit_id": self.unit_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind.value,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "visibility": self.visibility.value,
            "owner": self.owner,
            "type": self.type_name,
        }


class _SymbolCardSequence(Sequence[dict[str, object]]):
    def __init__(
        self,
        entries: Sequence[_SymbolEntry],
        max_chars: int,
    ) -> None:
        self._entries = entries
        self._max_chars = max_chars
        self._chunk_payload_chars = _card_chunk_payload_chars(max_chars)
        self._ends: list[int] = []
        total = 0
        for entry in entries:
            if (
                entry.card_json_upper_bound + 2 <= max_chars
                or entry.card_json_chars + 2 <= max_chars
            ):
                count = 1
            else:
                count = (
                    entry.card_json_chars + self._chunk_payload_chars - 1
                ) // self._chunk_payload_chars
            total += count
            self._ends.append(total)

    def __len__(self) -> int:
        return self._ends[-1] if self._ends else 0

    def __getitem__(
        self,
        index: int | slice,
    ) -> dict[str, object] | list[dict[str, object]]:
        if isinstance(index, slice):
            return [self[item_index] for item_index in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        entry_index = bisect_right(self._ends, index)
        entry = self._entries[entry_index]
        previous_end = self._ends[entry_index - 1] if entry_index else 0
        chunk_index = index - previous_end
        if (
            entry.card_json_upper_bound + 2 <= self._max_chars
            or entry.card_json_chars + 2 <= self._max_chars
        ):
            return entry.card()
        serialized = _compact_json(entry.card())
        chunk_count = self._ends[entry_index] - previous_end
        start = chunk_index * self._chunk_payload_chars
        return {
            "item_type": "card_chunk",
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "json": serialized[start:start + self._chunk_payload_chars],
        }


def _card_chunk_payload_chars(max_chars: int) -> int:
    wrapper_chars = len(
        _compact_json(
            {
                "item_type": "card_chunk",
                "chunk_index": 999999,
                "chunk_count": 999999,
                "json": "",
            }
        )
    ) + 2
    payload_chars = (max_chars - wrapper_chars) // 2
    if payload_chars < 1:
        raise AgentProtocolError(
            "item_too_large",
            "max_chars is too small for a structured response chunk.",
        )
    return payload_chars


@dataclass(frozen=True, slots=True)
class _Registry:
    project_id: str
    revision: str
    entries: tuple[_SymbolEntry, ...]
    by_target: dict[str, _SymbolEntry]
    sources: _SourceStore
    ranked_queries: OrderedDict[str, tuple[_SymbolEntry, ...]]
    static_retained_bytes: int


class _CpgCandidates(Mapping[str, tuple[CpgTarget, ...]]):
    def __init__(self, entries: Sequence[_SymbolEntry]) -> None:
        self._entries = entries
        self._resolved: dict[str, tuple[CpgTarget, ...]] = {}

    def __getitem__(self, name: str) -> tuple[CpgTarget, ...]:
        key = _normalized(name)
        cached = self._resolved.get(key)
        if cached is not None:
            return cached
        resolved = tuple(
            _cpg_target(entry)
            for entry in self._entries
            if entry.kind in _ROUTINE_KINDS
            and (
                entry.normalized_name == key
                or entry.normalized_qualified_name == key
            )
        )
        self._resolved[key] = resolved
        return resolved

    def __iter__(self):
        return iter(self._resolved)

    def __len__(self) -> int:
        return len(self._resolved)

    def resolve_many(
        self,
        names: set[str],
    ) -> dict[str, tuple[CpgTarget, ...]]:
        keys = {_normalized(name) for name in names}
        missing = keys.difference(self._resolved)
        if missing:
            collected: dict[str, list[CpgTarget]] = {
                name: [] for name in missing
            }
            for entry in self._entries:
                if entry.kind not in _ROUTINE_KINDS:
                    continue
                name_match = entry.normalized_name in missing
                qualified_match = (
                    entry.normalized_qualified_name in missing
                    and entry.normalized_qualified_name
                    != entry.normalized_name
                )
                if not name_match and not qualified_match:
                    continue
                target = _cpg_target(entry)
                if name_match:
                    collected[entry.normalized_name].append(target)
                if qualified_match:
                    collected[entry.normalized_qualified_name].append(target)
            self._resolved.update(
                (name, tuple(matches))
                for name, matches in collected.items()
            )
        return {
            name: self._resolved.get(name, ())
            for name in keys
        }


def _cpg_target(entry: _SymbolEntry) -> CpgTarget:
    return CpgTarget(
        target_id=entry.target_id,
        source_path=str(entry.source_path),
        path=entry.path,
        unit_id=entry.unit_id,
        name=entry.name,
        qualified_name=entry.qualified_name,
        kind=entry.kind.value,
        line=entry.line,
        column=entry.column,
        visibility=entry.visibility.value,
        type_name=entry.type_name,
        owner=entry.owner,
        parent_target_id=entry.parent_target_id,
    )


class AgentContext:
    def __init__(
        self,
        workspace: AgentWorkspace,
        *,
        workers: int = 0,
        worker_memory_budget_bytes: int | None = None,
        revision_check_interval_seconds: float = 0.0,
        navigation_cache_dir: str | Path | None = None,
        navigation_cache_max_bytes: int = _DEFAULT_NAVIGATION_CACHE_MAX_BYTES,
    ) -> None:
        self._workspace = workspace
        self._workers = workers
        self._worker_memory_budget_bytes = worker_memory_budget_bytes
        self._revision_check_interval_seconds = max(
            0.0,
            revision_check_interval_seconds,
        )
        self._last_revision_check_at = (
            time.monotonic() if self._revision_check_interval_seconds > 0.0 else 0.0
        )
        self._revision_epoch = 0
        self._parallel_stats = ParallelBuildStats(0, 0, 0, 0.0, 0)
        self._navigation_store = (
            NavigationShardStore(navigation_cache_dir)
            if navigation_cache_dir is not None
            else None
        )
        self._navigation_cache_max_bytes = max(0, navigation_cache_max_bytes)
        self._navigation_disk_hits = 0
        self._navigation_disk_misses = 0
        project_id = workspace.active_project_id
        self._focus = Focus(project_id=project_id) if project_id else Focus()
        self._last_revision = workspace.current_revision
        self._registry: _Registry | None = None
        self._relation_index: ProjectRelationIndex | None = None
        self._metrics: ProjectMetrics | None = None
        self._metrics_revision = ""
        cpg_budget_base = (
            worker_memory_budget_bytes
            if worker_memory_budget_bytes is not None
            else 512 * 1024**2
        )
        self._cpg_cache_limit = min(
            128 * 1024**2,
            max(1, cpg_budget_base // 5),
        )
        self._cpg_cache: OrderedDict[
            tuple[str, str, str, str, int],
            CpgSubgraph,
        ] = OrderedDict()
        self._cpg_cache_bytes = 0
        self._cpg_sources_parsed = 0
        self._prepared_response_cache: OrderedDict[
            tuple[str, str],
            Sequence[dict[str, object]],
        ] = OrderedDict()

    @classmethod
    def open(
        cls,
        root: str | Path,
        project_file: str | Path | None = None,
        *,
        workers: int = 0,
        worker_memory_budget_bytes: int | None = None,
        revision_check_interval_seconds: float = 0.0,
        navigation_cache_dir: str | Path | None = None,
        navigation_cache_max_bytes: int = _DEFAULT_NAVIGATION_CACHE_MAX_BYTES,
    ) -> AgentContext:
        return cls(
            AgentWorkspace.open(root, project_file=project_file),
            workers=workers,
            worker_memory_budget_bytes=worker_memory_budget_bytes,
            revision_check_interval_seconds=revision_check_interval_seconds,
            navigation_cache_dir=navigation_cache_dir,
            navigation_cache_max_bytes=navigation_cache_max_bytes,
        )

    @property
    def workspace(self) -> AgentWorkspace:
        return self._workspace

    @property
    def navigation_cache_is_warm(self) -> bool:
        return self._registry is not None

    @property
    def parallel_stats(self) -> ParallelBuildStats:
        return self._parallel_stats

    @property
    def navigation_disk_hits(self) -> int:
        return self._navigation_disk_hits

    @property
    def navigation_disk_misses(self) -> int:
        return self._navigation_disk_misses

    @property
    def cpg_cache_entries(self) -> int:
        return len(self._cpg_cache)

    @property
    def cpg_cache_bytes(self) -> int:
        return self._cpg_cache_bytes

    @property
    def cpg_sources_parsed(self) -> int:
        return self._cpg_sources_parsed

    def cache_roots(self) -> tuple[object, ...]:
        return (
            *self._workspace.cache_roots(),
            self._registry,
            self._relation_index,
            self._metrics,
            tuple(self._cpg_cache.values()),
            tuple(self._prepared_response_cache.values()),
        )

    @property
    def estimated_cache_bytes(self) -> int:
        retained = self._workspace.estimated_cache_bytes
        if self._registry is not None:
            retained += (
                self._registry.static_retained_bytes
                + self._registry.sources.retained_bytes
                + sum(
                    sys.getsizeof(query)
                    + sys.getsizeof(ranked)
                    + len(ranked) * 8
                    for query, ranked in self._registry.ranked_queries.items()
                )
            )
        if self._relation_index is not None:
            retained += self._relation_index.estimated_cache_bytes
        if self._metrics is not None:
            retained += 4096 + len(self._metrics.units) * 1024
        retained += self._cpg_cache_bytes
        retained += sum(
            sys.getsizeof(key)
            + sys.getsizeof(items)
            + len(items) * 8
            for key, items in self._prepared_response_cache.items()
        )
        return retained

    def evict_auxiliary_caches(self) -> None:
        self._relation_index = None
        self._metrics = None
        self._metrics_revision = ""
        self._clear_cpg_cache()
        self._prepared_response_cache.clear()
        if self._registry is not None:
            self._registry.sources.clear_loaded()

    def evict_navigation_caches(self) -> None:
        self.evict_auxiliary_caches()
        self._registry = None
        self._workspace.evict_recomputable_caches()

    def prewarm_navigation(self) -> str:
        revision = self._refresh_workspace("")
        self._require_registry(revision)
        return revision

    def invalidate_revision_cache(self) -> None:
        self._revision_epoch += 1
        self._last_revision_check_at = float("-inf")

    def handle(self, request: AgentRequest | Mapping[str, object]) -> AgentResponse:
        parsed = _validated_request(request)
        revision = self._refresh_workspace(parsed.project_id)

        if parsed.action == "cpg":
            registry = self._require_registry(revision)
            entry = self._resolve_target(registry, parsed.target_id)
            graph = self._require_cpg_subgraph(registry, entry, parsed)
            return self._response(
                parsed,
                revision,
                graph.to_items(),
                target_id=entry.target_id,
            )
        if parsed.action == "trace":
            if parsed.relation is None:
                raise AgentProtocolError("relation_required", "Trace requires a relation.")
            registry = self._require_registry(revision)
            entry = self._resolve_target(registry, parsed.target_id)
            relation_index = self._require_relation_index(registry, parsed.relation)
            items = relation_index.trace(entry.target_id, parsed.relation)
            return self._response(parsed, revision, items, target_id=entry.target_id)
        if parsed.action == "open":
            items = self._open_items()
            return self._response(parsed, revision, items)
        if parsed.action == "problems":
            self._require_selected_project()
            items = self._problem_items()
            return self._response(parsed, revision, items)
        if parsed.action == "metrics":
            return self._handle_metrics(parsed, revision)
        if parsed.action == "focus":
            return self._handle_focus(parsed, revision)
        if parsed.action == "find":
            registry = self._require_registry(revision)
            ranked = registry.ranked_queries.pop(parsed.query, None)
            if ranked is None:
                ranked = (
                    registry.entries
                    if not parsed.query.strip()
                    else tuple(_ranked_entries(registry.entries, parsed.query))
                )
            if len(ranked) <= _RANKED_QUERY_CACHE_MAX_ENTRIES:
                registry.ranked_queries[parsed.query] = ranked
                while len(registry.ranked_queries) > _RANKED_QUERY_CACHE_SIZE:
                    registry.ranked_queries.popitem(last=False)
            return self._response(
                parsed,
                revision,
                _SymbolCardSequence(ranked, parsed.max_chars),
                items_prepared=True,
            )
        if parsed.action == "inspect":
            registry = self._require_registry(revision)
            entry = self._resolve_target(registry, parsed.target_id)
            items = self._inspect_items(registry, entry, parsed)
            return self._response(parsed, revision, items, target_id=entry.target_id)
        raise AgentProtocolError("invalid_action", f"Unsupported action value: {parsed.action!r}.")

    def _refresh_workspace(self, requested_project_id: str) -> str:
        previous_project_id = self._workspace.active_project_id
        selected_project_id = requested_project_id or previous_project_id
        now = time.monotonic()
        revision_epoch = self._revision_epoch
        selection_changed = bool(
            requested_project_id
            and requested_project_id != previous_project_id
        )
        revision_is_fresh = (
            not selection_changed
            and self._revision_check_interval_seconds > 0.0
            and now - self._last_revision_check_at
            < self._revision_check_interval_seconds
        )
        if revision_is_fresh:
            revision = self._last_revision
        elif selected_project_id:
            revision = self._workspace._select_project_with_revision(selected_project_id)
        else:
            revision = self._workspace.workspace_revision
        if not revision_is_fresh and self._revision_epoch == revision_epoch:
            self._last_revision_check_at = now
        current_project_id = self._workspace.active_project_id

        if current_project_id != previous_project_id:
            self._registry = None
            self._relation_index = None
            self._metrics = None
            self._metrics_revision = ""
            self._clear_cpg_cache()
            self._prepared_response_cache.clear()
            self._focus = Focus(project_id=current_project_id) if current_project_id else Focus()
        elif revision != self._last_revision:
            self._registry = None
            self._relation_index = None
            self._metrics = None
            self._metrics_revision = ""
            self._clear_cpg_cache()
            self._prepared_response_cache.clear()
        elif self._focus.project_id != current_project_id:
            self._focus = Focus(project_id=current_project_id) if current_project_id else Focus()
        self._last_revision = revision
        return revision

    def _clear_cpg_cache(self) -> None:
        self._cpg_cache.clear()
        self._cpg_cache_bytes = 0

    def _require_cpg_subgraph(
        self,
        registry: _Registry,
        entry: _SymbolEntry,
        request: AgentRequest,
    ) -> CpgSubgraph:
        if entry.kind not in _TYPE_KINDS | _ROUTINE_KINDS | {SymbolKind.UNIT}:
            raise AgentProtocolError(
                "cpg_not_applicable",
                f"CPG does not apply to {entry.kind.value} target {entry.target_id}.",
            )
        key = (
            registry.revision,
            entry.target_id,
            request.graph,
            request.direction,
            request.depth,
        )
        cached = self._cpg_cache.pop(key, None)
        if cached is not None:
            self._cpg_cache[key] = cached
            return cached
        try:
            document = registry.sources[entry.source_path]
            self._cpg_sources_parsed += 1
            parsed = DelphiParser(
                defines=document.defines,
                include_paths=document.include_paths,
                include_loader=workspace_include_loader(
                    document.project_config,
                    document.include_paths,
                ),
                mode=(
                    ParserMode.TOLERANT
                    if len(self._workspace.units) >= 256
                    else ParserMode.STRICT
                ),
            ).parse(
                document.text,
                str(entry.source_path),
                build_semantic=False,
            )
            graph = build_cpg_subgraph(
                target=_cpg_target(entry),
                syntax_root=parsed.root,
                candidates=_CpgCandidates(registry.entries),
                graph=request.graph,
                direction=request.direction,
                depth=request.depth,
            )
        except AgentProtocolError:
            raise
        except (OSError, UnicodeError, KeyError):
            raise AgentProtocolError(
                "source_unavailable",
                f"Could not read selected source {entry.path}.",
            ) from None
        except Exception:
            raise AgentProtocolError(
                "cpg_build_failed",
                "Could not build the selected CPG subgraph.",
            ) from None
        if graph.retained_bytes <= self._cpg_cache_limit:
            while (
                self._cpg_cache
                and self._cpg_cache_bytes + graph.retained_bytes
                > self._cpg_cache_limit
            ):
                _, evicted = self._cpg_cache.popitem(last=False)
                self._cpg_cache_bytes -= evicted.retained_bytes
            self._cpg_cache[key] = graph
            self._cpg_cache_bytes += graph.retained_bytes
        return graph

    def _open_items(self) -> list[dict[str, object]]:
        active_project_id = self._workspace.active_project_id
        items: list[dict[str, object]] = []
        for project in self._workspace.projects:
            project_item = _sanitize_workspace_mapping(
                project.to_mapping(),
                self._workspace.root,
                path_namespace="project",
            )
            items.append(
                {
                    "item_type": "project",
                    **project_item,
                    "active": project.project_id == active_project_id,
                }
            )
        if not active_project_id:
            return items

        for unit in self._workspace.units:
            display_path = unit_display_path(self._workspace.root, unit)
            items.append(
                {
                    "item_type": "unit",
                    "unit_id": unit_target_id(self._workspace.root, unit),
                    "name": unit.name,
                    "path": display_path,
                    "has_error": unit.has_error,
                }
            )
        for include_file in self._workspace.include_files:
            items.append(
                {
                    "item_type": "include_file",
                    **_sanitize_workspace_mapping(
                        include_file,
                        self._workspace.root,
                        path_namespace="include",
                    ),
                }
            )
        for entry in self._workspace.search_path_entries:
            items.append(
                {
                    "item_type": "search_path",
                    **_sanitize_workspace_mapping(
                        entry,
                        self._workspace.root,
                        path_namespace="search-path",
                    ),
                }
            )
        for entry in self._workspace.include_path_entries:
            items.append(
                {
                    "item_type": "include_path",
                    **_sanitize_workspace_mapping(
                        entry,
                        self._workspace.root,
                        path_namespace="include-path",
                    ),
                }
            )
        for entry in self._workspace.define_entries:
            items.append(
                {
                    "item_type": "define",
                    **_sanitize_workspace_mapping(
                        entry,
                        self._workspace.root,
                        path_namespace="define",
                    ),
                }
            )
        items.extend(self._problem_items())
        return items

    def _problem_items(self) -> list[dict[str, object]]:
        return [
            {
                "item_type": "problem",
                **_sanitize_workspace_mapping(
                    problem,
                    self._workspace.root,
                    path_namespace="problem",
                ),
            }
            for problem in self._workspace.problems
        ]

    def _handle_focus(self, request: AgentRequest, revision: str) -> AgentResponse:
        if request.target_id:
            registry = self._require_registry(revision)
            entry = self._resolve_target(registry, request.target_id, allow_focused=False)
            self._focus = Focus(
                project_id=registry.project_id,
                unit_id=entry.unit_id,
                target_id=entry.target_id,
            )
        elif request.project_id:
            self._focus = Focus(project_id=self._workspace.active_project_id)
            self._require_registry(revision)
        return self._response(request, revision, [self._focus.to_mapping()])

    def _handle_metrics(self, request: AgentRequest, revision: str) -> AgentResponse:
        if request.detail not in {"summary", "members"}:
            raise AgentProtocolError(
                "invalid_detail",
                "Metrics supports only summary or members detail.",
            )
        metrics = self._require_metrics(revision)
        detail = request.detail == "members"
        if request.target_id:
            unit = next(
                (candidate for candidate in metrics.units if candidate.unit_id == request.target_id),
                None,
            )
            if unit is None:
                raise AgentProtocolError(
                    "target_not_found",
                    f"Target not found: {request.target_id}.",
                )
            return self._response(
                request,
                revision,
                [unit_metric_item(unit, detail=detail)],
                target_id=unit.unit_id,
            )

        units = metrics.units
        if request.query:
            query = request.query.casefold()
            units = tuple(
                unit
                for unit in units
                if query in unit.name.casefold() or query in unit.path.casefold()
            )
            items = [unit_metric_item(unit, detail=detail) for unit in units]
        else:
            items = [
                project_metric_item(metrics),
                *(unit_metric_item(unit, detail=detail) for unit in units),
            ]
        return self._response(request, revision, items)

    def _require_metrics(self, revision: str) -> ProjectMetrics:
        self._require_selected_project()
        if self._metrics is not None and self._metrics_revision == revision:
            return self._metrics
        self._metrics = build_workspace_metrics(
            self._workspace,
            workers=self._workers,
            worker_memory_budget_bytes=self._worker_memory_budget_bytes,
        )
        self._metrics_revision = revision
        return self._metrics

    def _require_registry(self, revision: str) -> _Registry:
        project_id = self._require_selected_project()
        if (
            self._registry is not None
            and self._registry.project_id == project_id
            and self._registry.revision == revision
        ):
            return self._registry
        (
            self._registry,
            self._parallel_stats,
            self._navigation_disk_hits,
            self._navigation_disk_misses,
        ) = _build_registry(
            self._workspace,
            project_id,
            revision,
            workers=self._workers,
            worker_memory_budget_bytes=self._worker_memory_budget_bytes,
            navigation_store=self._navigation_store,
            navigation_cache_max_bytes=self._navigation_cache_max_bytes,
        )
        if self._focus.target_id:
            focused_entry = self._registry.by_target.get(self._focus.target_id)
            if focused_entry is None:
                self._focus = Focus(project_id=project_id)
            else:
                self._focus = Focus(
                    project_id=project_id,
                    unit_id=focused_entry.unit_id,
                    target_id=focused_entry.target_id,
                )
        return self._registry

    def _require_relation_index(
        self,
        registry: _Registry,
        relation: str,
    ) -> ProjectRelationIndex:
        requires_complete_targets = relation not in {"uses", "used_by"}
        if (
            self._relation_index is not None
            and self._relation_index.project_id == registry.project_id
            and self._relation_index.revision == registry.revision
            and (
                not requires_complete_targets
                or self._relation_index.targets_complete
            )
        ):
            return self._relation_index
        targets = tuple(
            RelationTarget(
                target_id=entry.target_id,
                source_path=str(entry.source_path),
                path=entry.path,
                unit_id=entry.unit_id,
                unit_name=entry.unit_name,
                name=entry.name,
                qualified_name=entry.qualified_name,
                kind=entry.kind.value,
                signature=entry.signature,
                line=entry.line,
                column=entry.column,
                card={},
                visibility=entry.visibility.value,
                type_name=entry.type_name,
                owner=entry.owner,
            )
            for entry in registry.entries
            if requires_complete_targets or entry.kind == SymbolKind.UNIT
        )
        self._relation_index = ProjectRelationIndex(
            self._workspace,
            registry.project_id,
            registry.revision,
            targets,
            targets_complete=requires_complete_targets,
        )
        return self._relation_index

    def _require_selected_project(self) -> str:
        project_id = self._workspace.active_project_id
        if not project_id:
            raise AgentProtocolError(
                "project_required",
                "Multiple projects were found. Run 'query open', then select one "
                "with 'query focus --project-id PROJECT_ID'.",
            )
        return project_id

    def _resolve_target(
        self,
        registry: _Registry,
        target_id: str,
        *,
        allow_focused: bool = True,
    ) -> _SymbolEntry:
        resolved_id = target_id
        if not resolved_id and allow_focused:
            resolved_id = self._focus.target_id
        if not resolved_id:
            raise AgentProtocolError("target_required", "A target_id or focused target is required.")
        entry = registry.by_target.get(resolved_id)
        if entry is None:
            raise AgentProtocolError("target_not_found", f"Target not found: {resolved_id}.")
        return entry

    def _inspect_items(
        self,
        registry: _Registry,
        entry: _SymbolEntry,
        request: AgentRequest,
    ) -> list[dict[str, object]]:
        if request.detail == "summary":
            return [entry.card()]
        if request.detail == "members":
            return [
                candidate.card()
                for candidate in registry.entries
                if candidate.parent_target_id == entry.target_id
            ]

        document = registry.sources[entry.source_path]
        if request.detail == "declaration":
            start, end = _declaration_span(document, entry)
            return _source_items(
                document,
                start,
                end,
                request.max_chars,
                role="declaration",
                target_id=entry.target_id,
            )
        if request.detail == "context":
            declaration_start, declaration_end = _declaration_span(document, entry)
            start_line = max(1, document.line_col(declaration_start)[0] - 3)
            end_line = document.line_col(declaration_end)[0] + 5
            start = document.line_start(start_line)
            end = document.line_end(end_line, include_newline=True)
            return [entry.card(), *_source_items(
                document,
                start,
                end,
                request.max_chars,
                role="context",
                target_id=entry.target_id,
            )]
        if request.detail == "body":
            body_entry, span = _body_entry_and_span(registry, entry)
            if span is None:
                raise AgentProtocolError(
                    "body_unavailable",
                    f"No routine or type body is available for target: {entry.target_id}.",
                )
            body_document = registry.sources[body_entry.source_path]
            return _source_items(
                body_document,
                span[0],
                span[1],
                request.max_chars,
                role="body",
                target_id=body_entry.target_id,
            )
        if request.detail == "implementations":
            items: list[dict[str, object]] = []
            for counterpart in _matching_counterparts(registry, entry):
                card = counterpart.card()
                card["item_type"] = "counterpart"
                items.append(card)
                counterpart_document = registry.sources[counterpart.source_path]
                start, end = _declaration_span(counterpart_document, counterpart)
                items.extend(
                    _source_items(
                        counterpart_document,
                        start,
                        end,
                        request.max_chars,
                        role="counterpart_declaration",
                        target_id=counterpart.target_id,
                    )
                )
            return items
        raise AgentProtocolError(
            "invalid_detail",
            f"Unsupported detail value: {request.detail!r}.",
        )

    def _response(
        self,
        request: AgentRequest,
        revision: str,
        items: Sequence[dict[str, object]],
        *,
        target_id: str = "",
        items_prepared: bool = False,
    ) -> AgentResponse:
        fingerprint = _request_fingerprint(
            request,
            project_id=self._workspace.active_project_id,
            target_id=target_id or request.target_id,
        )
        cache_key = (revision, fingerprint)
        prepared = self._prepared_response_cache.pop(cache_key, None)
        if prepared is None:
            prepared = (
                items
                if items_prepared
                else _prepare_items(items, request.max_chars)
            )
        self._prepared_response_cache[cache_key] = prepared
        while len(self._prepared_response_cache) > _PREPARED_RESPONSE_CACHE_SIZE:
            self._prepared_response_cache.popitem(last=False)
        page, selected = paginate_items(
            prepared,
            revision,
            fingerprint,
            request.max_items,
            request.max_chars,
            request.cursor,
        )
        context_chars = len(_compact_json(selected))
        return AgentResponse(
            workspace_revision=revision,
            focus=self._focus,
            result=selected,
            page=page,
            context=ContextBudget(chars=context_chars),
        )


def _validated_request(request: AgentRequest | Mapping[str, object]) -> AgentRequest:
    if isinstance(request, AgentRequest):
        return AgentRequest.from_mapping(request.to_mapping())
    return AgentRequest.from_mapping(request)


def _build_registry(
    workspace: AgentWorkspace,
    project_id: str,
    revision: str,
    *,
    workers: int = 0,
    worker_memory_budget_bytes: int | None = None,
    navigation_store: NavigationShardStore | None = None,
    navigation_cache_max_bytes: int = _DEFAULT_NAVIGATION_CACHE_MAX_BYTES,
) -> tuple[_Registry, ParallelBuildStats, int, int]:
    build_started = time.perf_counter()
    raw_symbols: list[_RawSymbol] = []
    units = tuple(workspace.units)
    source_specs = {
        unit_source_path(workspace.root, unit): _SourceSpec(
            unit_source_path(workspace.root, unit),
            unit_display_path(workspace.root, unit),
            workspace.defines,
            workspace.include_paths,
            workspace.project_config,
        )
        for unit in units
    }
    tasks = tuple(
        _NavigationTask(
            ordinal,
            str(unit_source_path(workspace.root, unit)),
            unit_display_path(workspace.root, unit),
            unit.name,
            unit.path,
            unit.unit_id,
            unit.has_error,
            workspace.defines,
            workspace.include_paths,
        )
        for ordinal, unit in enumerate(units)
    )

    def consume_result(result: _NavigationResult) -> None:
        if result.read_error:
            unit = units[result.ordinal]
            display_path = unit_display_path(workspace.root, unit)
            raise AgentProtocolError(
                "source_unavailable",
                f"Could not read selected source {display_path}.",
            )
        if navigation_store is not None and result.cache_key:
            try:
                navigation_store.store(
                    result.cache_key,
                    _navigation_shard_payload(result),
                )
            except (OSError, TypeError, ValueError):
                pass
        raw_symbols.extend(result.raw_symbols)

    disk_hits = 0
    disk_misses = 0
    live_navigation_keys: set[str] = set()
    pending_tasks: list[_NavigationTask] = []
    if navigation_store is None:
        pending_tasks.extend(tasks)
    else:
        for task in tasks:
            try:
                text = read_source_text(Path(task.source_path))
                cache_key = navigation_cache_key(text, task.defines)
                live_navigation_keys.add(cache_key)
                payload = navigation_store.load(cache_key)
            except (OSError, UnicodeError, ValueError):
                cache_key = ""
                payload = None
            cached = (
                _navigation_result_from_shard(task, payload)
                if payload is not None
                else None
            )
            if cached is None:
                disk_misses += 1
                pending_tasks.append(replace(task, cache_key=cache_key or "miss"))
                continue
            disk_hits += 1
            consume_result(cached)

    outline_batch = run_outline_tasks(
        pending_tasks,
        configured_workers=workers,
        memory_budget_bytes=worker_memory_budget_bytes,
        on_complete=consume_result,
        retain_results=False,
        task_runner=_parse_navigation_task,
    )
    if navigation_store is not None:
        try:
            navigation_store.prune(
                live_navigation_keys,
                navigation_cache_max_bytes,
            )
        except (OSError, ValueError):
            pass
    parallel_stats = replace(
        outline_batch.stats,
        files_completed=len(tasks),
        elapsed_seconds=time.perf_counter() - build_started,
    )

    ordered = sorted(raw_symbols, key=_raw_sort_key)
    overload_groups: dict[tuple[str, str, str, str], list[_RawSymbol]] = {}
    for raw in raw_symbols:
        identity = (
            raw.kind.value.casefold(),
            raw.path.casefold(),
            _normalized(raw.qualified_name),
            _normalized(raw.signature),
        )
        overload_groups.setdefault(identity, []).append(raw)
    ordinals: dict[int, int] = {}
    for group in overload_groups.values():
        overload_order = sorted(
            group,
            key=lambda raw: (
                raw.line,
                raw.column,
                _raw_sort_key(raw),
            ),
        )
        for ordinal, raw in enumerate(overload_order):
            ordinals[id(raw)] = ordinal

    shared_strings: dict[str, str] = {}

    def shared(value: str) -> str:
        return shared_strings.setdefault(value, value)

    target_ids = {
        id(raw): make_target_id(
            raw.kind.value,
            raw.path,
            _target_identity_name(raw),
            ordinals[id(raw)],
        )
        for raw in raw_symbols
    }
    parent_ids: dict[str, str] = {}
    for raw in raw_symbols:
        if raw.kind in {
            SymbolKind.CLASS,
            SymbolKind.RECORD,
            SymbolKind.INTERFACE,
            SymbolKind.TYPE,
        }:
            parent_ids.setdefault(
                _normalized(raw.qualified_name),
                target_ids[id(raw)],
            )

    entries: list[_SymbolEntry] = []
    for raw in ordered:
        ordinal = ordinals[id(raw)]
        (
            normalized_name,
            normalized_qualified_name,
            relative_name_offset,
        ) = _normalized_search_fields(
            raw.name,
            raw.qualified_name,
            raw.unit_name,
        )
        entry = _SymbolEntry(
            name=shared(raw.name),
            kind=raw.kind,
            line=raw.line,
            column=raw.column,
            visibility=raw.visibility,
            type_name=shared(raw.type_name),
            source_path=raw.source_path,
            path=shared(raw.path),
            unit_id=shared(raw.unit_id),
            unit_name=shared(raw.unit_name),
            qualified_name=shared(raw.qualified_name),
            normalized_name=shared(normalized_name),
            normalized_qualified_name=shared(normalized_qualified_name),
            relative_name_offset=relative_name_offset,
            owner=shared(raw.owner),
            signature=shared(raw.signature),
            ordinal=ordinal,
            target_id=target_ids[id(raw)],
            card_json_chars=0,
            card_json_upper_bound=0,
            parent_target_id=parent_ids.get(
                _normalized(raw.parent_qualified_name),
                "",
            ),
        )
        entries.append(
            replace(
                entry,
                card_json_chars=len(_compact_json(entry.card())),
                card_json_upper_bound=_symbol_card_json_upper_bound(entry),
            )
        )

    entries_tuple = tuple(sorted(entries, key=_entry_sort_key))
    source_cache_bytes = _source_cache_budget(worker_memory_budget_bytes)
    sources = _SourceStore(source_specs, max_loaded_bytes=source_cache_bytes)
    static_retained_bytes = _estimate_registry_bytes(entries_tuple, sources)
    return (
        _Registry(
            project_id=project_id,
            revision=revision,
            entries=entries_tuple,
            by_target={entry.target_id: entry for entry in entries_tuple},
            sources=sources,
            ranked_queries=OrderedDict(),
            static_retained_bytes=static_retained_bytes,
        ),
        parallel_stats,
        disk_hits,
        disk_misses,
    )


def _source_cache_budget(total_budget_bytes: int | None) -> int:
    if total_budget_bytes is None:
        return 64 * 1024**2
    return max(0, min(128 * 1024**2, total_budget_bytes // 4))


def _estimate_registry_bytes(
    entries: tuple[_SymbolEntry, ...],
    sources: _SourceStore,
) -> int:
    retained = 4096 + sources.metadata_bytes + sys.getsizeof(entries)
    seen_values: set[int] = set()
    for entry in entries:
        retained += 384 + sys.getsizeof(entry)
        for value in (
            entry.source_path,
            entry.name,
            entry.type_name,
            entry.path,
            entry.unit_id,
            entry.unit_name,
            entry.qualified_name,
            entry.normalized_name,
            entry.normalized_qualified_name,
            entry.owner,
            entry.signature,
            entry.target_id,
            entry.parent_target_id,
        ):
            identifier = id(value)
            if identifier in seen_values:
                continue
            seen_values.add(identifier)
            retained += sys.getsizeof(value)
    retained += len(entries) * 96
    return retained


def _symbol_card_json_upper_bound(entry: _SymbolEntry) -> int:
    strings = (
        entry.target_id,
        entry.unit_id,
        entry.name,
        entry.qualified_name,
        entry.kind.value,
        entry.path,
        entry.visibility.value,
        entry.owner,
        entry.type_name,
    )
    numeric_chars = len(str(entry.line)) + len(str(entry.column))
    # JSON string escaping expands one input character to at most six characters.
    # The fixed allowance covers keys, quotes, separators, brackets, and numbers.
    return 512 + 6 * sum(len(value) for value in strings) + numeric_chars


def _stable_path_component(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\\", "_").replace("/", "_")
    return normalized or "unknown"


def _sanitize_workspace_mapping(
    value: Mapping[str, object],
    root: Path,
    *,
    path_namespace: str,
) -> dict[str, object]:
    sanitized = dict(value)
    path = sanitized.get("path")
    if isinstance(path, str):
        sanitized["path"] = _sanitize_workspace_path(path, root, path_namespace)
    origin = sanitized.get("origin")
    if isinstance(origin, str):
        sanitized["origin"] = _sanitize_workspace_path(origin, root, "origin")
    origins = sanitized.get("origins")
    if isinstance(origins, (list, tuple)):
        sanitized["origins"] = [
            _sanitize_workspace_path(item, root, "origin") if isinstance(item, str) else item
            for item in origins
        ]
    return sanitized


def _sanitize_workspace_path(value: str, root: Path, namespace: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    native_path = Path(value).expanduser()
    if native_path.is_absolute():
        resolved = native_path.resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            component = resolved.name
    else:
        windows_path = PureWindowsPath(value)
        if not windows_path.is_absolute():
            return normalized
        component = windows_path.name or windows_path.drive.rstrip(":\\/")
    return f"@external/{namespace}/{_stable_path_component(component)}"


def _target_identity_name(raw: _RawSymbol) -> str:
    if raw.kind in _ROUTINE_KINDS:
        return f"{raw.qualified_name}\x1f{_normalized(raw.signature)}"
    return raw.qualified_name


def _collect_raw_symbols(
    scope: Scope,
    unit: AgentUnit,
    source_path: Path,
    document: _SourceDocument,
) -> list[_RawSymbol]:
    unit_name = scope.name or unit.name
    unit_id = make_target_id("unit", document.display_path, unit.name)
    collected: list[_RawSymbol] = []
    symbols = [symbol for group in scope.symbols.values() for symbol in group]
    symbols.sort(key=_symbol_sort_key)
    for symbol in symbols:
        if symbol.scope.kind != ScopeKind.UNIT:
            continue
        _correct_outline_symbol_kind(document, symbol)
        if symbol.kind == SymbolKind.UNIT:
            qualified_name = unit_name
            owner = ""
        else:
            declared_name = _declared_symbol_name(document, symbol).strip(".")
            qualified_name = f"{unit_name}.{declared_name}"
            owner = qualified_name.rsplit(".", 1)[0]
        collected.append(
            _RawSymbol(
                name=symbol.name,
                kind=symbol.kind,
                line=symbol.decl_range.start_line,
                column=symbol.decl_range.start_col,
                visibility=symbol.visibility,
                type_name=symbol.type_ref.display_name(),
                source_path=source_path,
                path=document.display_path,
                unit_id=unit_id,
                unit_name=unit_name,
                qualified_name=qualified_name,
                owner=owner,
                parent_qualified_name="",
                signature=_symbol_signature(document, symbol),
            )
        )
        member_scope = symbol.member_scope
        if member_scope is None or member_scope.kind != ScopeKind.TYPE:
            continue
        members = [member for group in member_scope.symbols.values() for member in group]
        members.sort(key=_symbol_sort_key)
        for member in members:
            member_name = _declared_symbol_name(document, member).strip(".")
            member_qualified_name = f"{qualified_name}.{member_name}"
            collected.append(
                _RawSymbol(
                    name=member.name,
                    kind=member.kind,
                    line=member.decl_range.start_line,
                    column=member.decl_range.start_col,
                    visibility=member.visibility,
                    type_name=member.type_ref.display_name(),
                    source_path=source_path,
                    path=document.display_path,
                    unit_id=unit_id,
                    unit_name=unit_name,
                    qualified_name=member_qualified_name,
                    owner=qualified_name,
                    parent_qualified_name=qualified_name,
                    signature=_symbol_signature(document, member),
                )
            )
    return collected


def _correct_outline_symbol_kind(document: _SourceDocument, symbol: Symbol) -> None:
    if symbol.kind != SymbolKind.CONSTANT:
        return
    start = _declaration_start(document, symbol.decl_range.start_line)
    if _declaration_section(document, start) != "type":
        return
    token_index = document.first_token_index(start)
    equals_index = _next_token_value(document.tokens, token_index, "=")
    if equals_index is None or equals_index + 1 >= len(document.tokens):
        return
    rhs = document.tokens[equals_index + 1:]
    index = 0
    if rhs and rhs[0].value == "packed":
        index = 1
    if index >= len(rhs):
        return
    value = rhs[index].value
    if value == "class" and not (
        index + 1 < len(rhs) and rhs[index + 1].value == "of"
    ):
        symbol.kind = SymbolKind.CLASS
    elif value == "record":
        symbol.kind = SymbolKind.RECORD
    elif value == "interface":
        symbol.kind = SymbolKind.INTERFACE
    elif value == "(":
        symbol.kind = SymbolKind.ENUM
    else:
        symbol.kind = SymbolKind.TYPE


def _advance_declaration_section(
    state: tuple[str, int, int, int],
    token: _Token,
) -> tuple[str, int, int, int]:
    section, parentheses, brackets, angles = state
    if token.directive:
        return state
    if token.value == "(":
        parentheses += 1
    elif token.value == ")":
        parentheses = max(0, parentheses - 1)
    elif token.value == "[":
        brackets += 1
    elif token.value == "]":
        brackets = max(0, brackets - 1)
    elif token.value == "<":
        angles += 1
    elif token.value == ">":
        angles = max(0, angles - 1)
    elif not parentheses and not brackets and not angles and token.word:
        if token.value in {"const", "resourcestring", "threadvar", "type", "var"}:
            section = token.value
        elif token.value in {"implementation", "initialization", "finalization"}:
            section = ""
    return section, parentheses, brackets, angles


def _declaration_section(document: _SourceDocument, offset: int) -> str:
    target_index = document.first_token_index(offset)
    cached = document.declaration_section_checkpoints.get(target_index)
    if cached is not None:
        return cached[0]
    checkpoint_position = bisect_right(document.declaration_section_indexes, target_index) - 1
    checkpoint_index = document.declaration_section_indexes[checkpoint_position]
    state = document.declaration_section_checkpoints[checkpoint_index]
    for token in document.tokens[checkpoint_index:target_index]:
        state = _advance_declaration_section(state, token)
    insert_at = bisect_left(document.declaration_section_indexes, target_index)
    document.declaration_section_indexes.insert(insert_at, target_index)
    document.declaration_section_checkpoints[target_index] = state
    return state[0]


def _declared_symbol_name(document: _SourceDocument, symbol: Symbol) -> str:
    if symbol.kind in _ROUTINE_KINDS:
        return _routine_declared_name(document, symbol) or symbol.name
    if symbol.kind in _TYPE_KINDS:
        return _type_declared_name(document, symbol) or symbol.name
    return symbol.name


def _routine_declared_name(document: _SourceDocument, symbol: Symbol) -> str:
    start = _declaration_start(document, symbol.decl_range.start_line)
    token_index = document.first_token_index(start)
    routine_index = _routine_keyword_index(document.tokens, token_index)
    if routine_index is None:
        return ""
    heading_end = _heading_semicolon_index(document.tokens, routine_index)
    if heading_end is None:
        return ""
    name_span = _routine_name_token_span(document.tokens, routine_index, heading_end)
    if name_span is None:
        return ""
    return _join_source_tokens(document, document.tokens[name_span[0]:name_span[1]])


def _type_declared_name(document: _SourceDocument, symbol: Symbol) -> str:
    start = _declaration_start(document, symbol.decl_range.start_line)
    token_index = document.first_token_index(start)
    equals_index = next(
        (
            index
            for index in range(token_index, len(document.tokens))
            if document.tokens[index].value in {"=", ";"}
        ),
        None,
    )
    if equals_index is None or document.tokens[equals_index].value != "=":
        return ""
    name_index = next(
        (
            index
            for index in range(token_index, equals_index)
            if document.tokens[index].word
            and _normalized(document.tokens[index].value) == _normalized(symbol.name)
        ),
        token_index,
    )
    return _join_source_tokens(document, document.tokens[name_index:equals_index])


def _join_source_tokens(document: _SourceDocument, tokens: tuple[_Token, ...]) -> str:
    return unicodedata.normalize(
        "NFC",
        "".join(document.text[token.start:token.end] for token in tokens),
    )


def _symbol_signature(document: _SourceDocument, symbol: Symbol) -> str:
    if symbol.kind not in _ROUTINE_KINDS:
        return ""
    start = _declaration_start(document, symbol.decl_range.start_line)
    start_index = document.first_token_index(start)
    routine_index = _routine_keyword_index(document.tokens, start_index)
    if routine_index is None:
        return ""
    heading_end = _heading_semicolon_index(document.tokens, routine_index)
    if heading_end is None:
        return ""
    name_span = _routine_name_token_span(document.tokens, routine_index, heading_end)
    if name_span is None:
        return ""
    signature_tokens = document.tokens[name_span[1]:heading_end]
    declaration_end = _routine_declaration_end_index(document.tokens, routine_index)
    calling_conventions = ""
    if declaration_end is not None:
        conventions = sorted(
            {
                token.value
                for token in document.tokens[heading_end + 1:declaration_end]
                if token.word and not token.escaped and token.value in _CALLING_CONVENTIONS
            }
        )
        if conventions:
            calling_conventions = f"|cc:{','.join(conventions)}"
    return _normalized(f"{_normalize_routine_signature(signature_tokens)}{calling_conventions}")


def _routine_name_token_span(
    tokens: tuple[_Token, ...],
    routine_index: int,
    heading_end: int,
) -> tuple[int, int] | None:
    start = routine_index + 1
    if start >= heading_end:
        return None
    end = start + 1
    while end < heading_end:
        if (
            tokens[end].value == "."
            and end + 1 < heading_end
            and (tokens[end + 1].word or tokens[routine_index].value == "operator")
        ):
            end += 2
            continue
        if tokens[end].value == "<":
            generic_end = _matching_token_index(tokens, end, "<", ">")
            if (
                generic_end is not None
                and generic_end + 1 < heading_end
                and tokens[generic_end + 1].value == "."
            ):
                end = generic_end + 1
                continue
        break
    return start, end


def _normalize_routine_signature(tokens: tuple[_Token, ...]) -> str:
    open_index = next((index for index, token in enumerate(tokens) if token.value == "("), None)
    if open_index is None:
        colon = _top_level_token_index(tokens, ":")
        if colon is None:
            return f"{_join_token_values(tokens)}()"
        generic = _join_token_values(tokens[:colon])
        return f"{generic}():{_normalize_type_tokens(tokens[colon + 1:])}"

    close_index = _matching_token_index(tokens, open_index, "(", ")")
    if close_index is None:
        return _join_token_values(tokens)
    generic = _join_token_values(tokens[:open_index])
    parameters = _normalize_parameters(tokens[open_index + 1:close_index])
    result_type = _normalize_result_type(tokens[close_index + 1:])
    return f"{generic}({parameters}){result_type}"


def _normalize_parameters(tokens: tuple[_Token, ...]) -> str:
    groups = _split_tokens_at_top_level(tokens, ";")
    normalized: list[str] = []
    modes = {"const", "constref", "out", "var"}
    for group in groups:
        if not group:
            continue
        colon = _top_level_token_index(group, ":")
        if colon is None:
            normalized.append(_join_token_values(group))
            continue
        names = list(group[:colon])
        mode = ""
        if names and names[0].word and names[0].value in modes:
            mode = names.pop(0).value
        parameter_count = 1 + sum(token.value == "," for token in names)
        type_tokens = group[colon + 1:]
        default = _top_level_token_index(type_tokens, "=")
        if default is not None:
            type_tokens = type_tokens[:default]
        normalized.append(f"{mode}#{parameter_count}:{_normalize_type_tokens(type_tokens)}")
    return ";".join(normalized)


def _normalize_result_type(tokens: tuple[_Token, ...]) -> str:
    if not tokens or tokens[0].value != ":":
        return ""
    return f":{_normalize_type_tokens(tokens[1:])}"


def _normalize_type_tokens(tokens: tuple[_Token, ...]) -> str:
    parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        parts.append(_normalized(token.value))
        if (
            token.word
            and token.value in {"procedure", "function"}
            and index + 1 < len(tokens)
            and tokens[index + 1].value == "("
        ):
            close = _matching_token_index(tokens, index + 1, "(", ")")
            if close is None:
                index += 1
                continue
            parts.append("(")
            parts.append(_normalize_parameters(tokens[index + 2:close]))
            parts.append(")")
            index = close + 1
            continue
        index += 1
    return unicodedata.normalize("NFC", "".join(parts)).casefold()


def _split_tokens_at_top_level(
    tokens: tuple[_Token, ...],
    separator: str,
) -> list[tuple[_Token, ...]]:
    groups: list[tuple[_Token, ...]] = []
    start = 0
    depths = {"(": 0, "[": 0, "<": 0}
    closing = {")": "(", "]": "[", ">": "<"}
    for index, token in enumerate(tokens):
        if token.value in depths:
            depths[token.value] += 1
        elif token.value in closing:
            opener = closing[token.value]
            depths[opener] = max(0, depths[opener] - 1)
        elif token.value == separator and not any(depths.values()):
            groups.append(tokens[start:index])
            start = index + 1
    groups.append(tokens[start:])
    return groups


def _top_level_token_index(tokens: tuple[_Token, ...], value: str) -> int | None:
    depths = {"(": 0, "[": 0, "<": 0}
    closing = {")": "(", "]": "[", ">": "<"}
    for index, token in enumerate(tokens):
        if token.value in depths:
            depths[token.value] += 1
        elif token.value in closing:
            opener = closing[token.value]
            depths[opener] = max(0, depths[opener] - 1)
        elif token.value == value and not any(depths.values()):
            return index
    return None


def _matching_token_index(
    tokens: tuple[_Token, ...],
    start: int,
    opener: str,
    closer: str,
) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].value == opener:
            depth += 1
        elif tokens[index].value == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _join_token_values(tokens: tuple[_Token, ...]) -> str:
    return unicodedata.normalize("NFC", "".join(token.value for token in tokens)).casefold()


def _exclude_routine_locals(
    symbols: list[_RawSymbol],
    document: _SourceDocument,
) -> list[_RawSymbol]:
    containers: list[tuple[int, int, _RawSymbol]] = []
    for raw in symbols:
        if raw.parent_qualified_name or raw.kind not in _ROUTINE_KINDS:
            continue
        span = _raw_routine_span(raw, document)
        if span is not None:
            containers.append((span[0], span[1], raw))
    if not containers:
        return symbols

    containers.sort(key=lambda item: (item[0], item[1]))
    positioned = sorted(
        (
            document.offset(
                raw.line,
                raw.column,
            ),
            order,
            raw,
        )
        for order, raw in enumerate(symbols)
    )
    active_ends: list[tuple[int, int, int]] = []
    active_ids: set[int] = set()
    excluded_orders: set[int] = set()
    container_index = 0
    for offset, order, raw in positioned:
        while (
            container_index < len(containers)
            and containers[container_index][0] < offset
        ):
            _, end, container = containers[container_index]
            container_id = id(container)
            heappush(active_ends, (end, container_index, container_id))
            active_ids.add(container_id)
            container_index += 1
        while active_ends and active_ends[0][0] <= offset:
            _, _, container_id = heappop(active_ends)
            active_ids.discard(container_id)
        raw_id = id(raw)
        if active_ids and (raw_id not in active_ids or len(active_ids) > 1):
            excluded_orders.add(order)

    return [
        raw
        for order, raw in enumerate(symbols)
        if order not in excluded_orders
    ]


def _raw_routine_span(
    raw: _RawSymbol,
    document: _SourceDocument,
) -> tuple[int, int] | None:
    if raw.kind not in _ROUTINE_KINDS or raw.parent_qualified_name:
        return None
    line = raw.line
    if document.unit_kind == "unit" and (
        not document.implementation_line or line < document.implementation_line
    ):
        return None
    start = _declaration_start(document, line)
    return _routine_span(document, start)


def _body_entry_and_span(
    registry: _Registry,
    entry: _SymbolEntry,
) -> tuple[_SymbolEntry, tuple[int, int] | None]:
    candidates = [entry]
    if entry.kind in _ROUTINE_KINDS:
        candidates.extend(_matching_counterparts(registry, entry))
    for candidate in candidates:
        document = registry.sources[candidate.source_path]
        span = _entry_body_span(candidate, document)
        if span is not None:
            return candidate, span
    return entry, None


def _matching_counterparts(
    registry: _Registry,
    entry: _SymbolEntry,
) -> list[_SymbolEntry]:
    return [
        candidate
        for candidate in registry.entries
        if candidate.target_id != entry.target_id
        and candidate.kind == entry.kind
        and _normalized(candidate.qualified_name) == _normalized(entry.qualified_name)
        and candidate.signature == entry.signature
    ]


def _entry_body_span(
    entry: _SymbolEntry,
    document: _SourceDocument,
) -> tuple[int, int] | None:
    if entry.kind in _TYPE_KINDS:
        if _is_forward_type(document, entry):
            return None
        span = _type_span(document, entry)
        if span is not None and not document.contains_directive(*span):
            return span
        return _full_parser_span(document, entry)
    if entry.kind not in _ROUTINE_KINDS or entry.parent_target_id:
        return None
    line = entry.line
    if document.unit_kind == "unit" and (
        not document.implementation_line or line < document.implementation_line
    ):
        return None
    start = _declaration_start(document, line)
    span = _routine_span(document, start)
    if span is not None and not document.contains_directive(*span):
        return span
    return _full_parser_span(document, entry)


def _full_parser_span(
    document: _SourceDocument,
    entry: _SymbolEntry,
) -> tuple[int, int] | None:
    if entry.target_id in document.parser_spans:
        return document.parser_spans[entry.target_id]
    result = document.full_parse()
    if result is None:
        document.parser_spans[entry.target_id] = None
        return None

    expected_type = (
        SyntaxNodeType.ntMethod
        if entry.kind in _ROUTINE_KINDS
        else SyntaxNodeType.ntTypeDecl
    )
    expected_line = entry.line
    candidates: list[tuple[int, int]] = []
    for node in _walk_syntax_nodes(result.root):
        if node.typ != expected_type or not isinstance(node, CompoundSyntaxNode):
            continue
        mapped = _mapped_tree_span(document, result, node)
        if mapped is None or document.line_col(mapped[0])[0] != expected_line:
            continue
        if expected_type == SyntaxNodeType.ntMethod and not _syntax_method_has_body(node):
            continue
        candidates.append(mapped)

    span = candidates[0] if len(candidates) == 1 else None
    document.parser_spans[entry.target_id] = span
    return span


def _walk_syntax_nodes(root: SyntaxNode):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.child_nodes))


def _syntax_method_has_body(node: SyntaxNode) -> bool:
    if any(
        node.has_attribute(attribute)
        for attribute in (
            AttributeName.anAbstract,
            AttributeName.anExternal,
            AttributeName.anForwarded,
        )
    ):
        return False
    return any(
        candidate.typ == SyntaxNodeType.ntStatements
        for candidate in _walk_syntax_nodes(node)
        if candidate is not node
    )


def _mapped_tree_span(document: _SourceDocument, result, node: SyntaxNode) -> tuple[int, int] | None:
    end_line, end_col = _syntax_tree_end(node)
    start_file, start_line, _ = result.preprocessed.map_position(node.line, node.col)
    end_file, mapped_end_line, mapped_end_col = result.preprocessed.map_position(end_line, end_col)
    if not start_file or not end_file:
        return None
    try:
        start_path = Path(start_file).expanduser().resolve()
        end_path = Path(end_file).expanduser().resolve()
    except OSError:
        return None
    if start_path != document.source_path or end_path != document.source_path:
        return None

    previous_line = start_line - 1
    for preprocessed_line in range(node.line, end_line + 1):
        mapped_file, mapped_line, _ = result.preprocessed.map_position(preprocessed_line, 1)
        if not mapped_file or Path(mapped_file).expanduser().resolve() != document.source_path:
            return None
        if mapped_line != previous_line + 1:
            return None
        previous_line = mapped_line

    start = _declaration_start(document, start_line)
    end = document.offset(mapped_end_line, mapped_end_col)
    if end < len(document.text) and not document.text[end].isspace():
        end += 1
    if end <= start:
        return None
    return start, end


def _syntax_tree_end(node: SyntaxNode) -> tuple[int, int]:
    end = (
        (node.end_line, node.end_col)
        if isinstance(node, CompoundSyntaxNode)
        else (node.line, node.col)
    )
    for child in node.child_nodes:
        end = max(end, _syntax_tree_end(child))
    return end


def _declaration_span(
    document: _SourceDocument,
    entry: _SymbolEntry,
) -> tuple[int, int]:
    line = entry.line
    start = _declaration_start(document, line)
    if entry.kind in _TYPE_KINDS:
        full_span, direct_structured = _type_declaration_layout(document, entry)
        if direct_structured:
            return start, document.line_end(line)
        if full_span is not None:
            return full_span
        return start, document.line_end(line)

    token_index = document.first_token_index(start)
    if entry.kind in _ROUTINE_KINDS:
        routine_index = _routine_keyword_index(document.tokens, token_index)
        if routine_index is not None:
            declaration_end = _routine_declaration_end_index(document.tokens, routine_index)
            if declaration_end is not None:
                return start, document.tokens[declaration_end].end
    for token in document.tokens[token_index:]:
        if token.value == ";":
            return start, token.end
    return start, document.line_end(line)


def _declaration_start(document: _SourceDocument, line: int) -> int:
    start = document.line_start(line)
    end = document.line_end(line)
    while start < end and document.text[start] in {" ", "\t"}:
        start += 1
    return start


def _type_span(
    document: _SourceDocument,
    entry: _SymbolEntry,
) -> tuple[int, int] | None:
    declaration, _ = _type_declaration_layout(document, entry)
    return declaration


def _is_forward_type(document: _SourceDocument, entry: _SymbolEntry) -> bool:
    start = _declaration_start(document, entry.line)
    token_index = document.first_token_index(start)
    equals_index = _next_token_value(document.tokens, token_index, "=")
    if equals_index is None:
        return False
    structure_index = _structured_type_index(document.tokens, equals_index + 1)
    return (
        structure_index is not None
        and structure_index + 1 < len(document.tokens)
        and document.tokens[structure_index + 1].value == ";"
    )


def _type_declaration_layout(
    document: _SourceDocument,
    entry: _SymbolEntry,
) -> tuple[tuple[int, int] | None, bool]:
    start = _declaration_start(document, entry.line)
    token_index = document.first_token_index(start)
    equals_index = _next_token_value(document.tokens, token_index, "=")
    if equals_index is None:
        return None, False
    structure_index = _structured_type_index(document.tokens, equals_index + 1)
    if structure_index is not None:
        direct_structured = all(
            token.value in {"packed"}
            for token in document.tokens[equals_index + 1:structure_index]
        )
        if (
            structure_index + 1 < len(document.tokens)
            and document.tokens[structure_index + 1].value == ";"
        ):
            return (start, document.tokens[structure_index + 1].end), True
        end = _match_end_terminated_block(document.tokens, structure_index)
        return ((start, end) if end is not None else None), direct_structured

    declaration_end = _top_level_semicolon_index(document.tokens, equals_index + 1)
    if declaration_end is None:
        return None, False
    return (start, document.tokens[declaration_end].end), False


def _next_token_value(
    tokens: tuple[_Token, ...],
    start_index: int,
    value: str,
) -> int | None:
    for index in range(start_index, len(tokens)):
        if tokens[index].value == value:
            return index
        if tokens[index].value == ";":
            return None
    return None


def _structured_type_index(
    tokens: tuple[_Token, ...],
    start_index: int,
) -> int | None:
    for index in range(start_index, len(tokens)):
        token = tokens[index]
        if token.value == ";":
            return None
        if (
            token.word
            and not token.escaped
            and token.value in _STRUCTURED_TYPE_WORDS
            and _is_structured_type_opener(tokens, index)
        ):
            return index
    return None


def _routine_span(
    document: _SourceDocument,
    start: int,
) -> tuple[int, int] | None:
    cached = document.routine_spans.get(start, ...)
    if cached is not ...:
        return cached
    token_index = document.first_token_index(start)
    found = _find_routine_token_span(
        document.tokens,
        document.token_starts,
        token_index,
        cache=document.routine_token_spans,
    )
    span = (start, found[1]) if found is not None else None
    document.routine_spans[start] = span
    return span


def _find_routine_token_span(
    tokens: tuple[_Token, ...],
    token_starts: tuple[int, ...],
    start_index: int,
    *,
    cache: dict[int, tuple[int, int, int] | None] | None = None,
    depth: int = 0,
) -> tuple[int, int, int] | None:
    if depth > 64:
        return None
    routine_index = _routine_keyword_index(tokens, start_index)
    if routine_index is None:
        return None
    spans = cache if cache is not None else {}
    missing = object()
    cached = spans.get(routine_index, missing)
    if cached is not missing:
        if cached is None:
            return None
        return tokens[start_index].start, cached[1], cached[2]

    heading_end = _heading_semicolon_index(tokens, routine_index)
    if heading_end is None:
        spans[routine_index] = None
        return None

    frames: list[list[int]] = [[routine_index, heading_end + 1]]

    def reject_active_frames() -> None:
        for active_routine_index, _ in frames:
            spans[active_routine_index] = None
        frames.clear()

    while frames:
        frame = frames[-1]
        frame_routine_index, index = frame
        if index >= len(tokens):
            reject_active_frames()
            continue

        token = tokens[index]
        if token.directive:
            reject_active_frames()
            continue
        if token.word and not token.escaped:
            if token.value in _NO_BODY_DIRECTIVES:
                spans[frame_routine_index] = None
                frames.pop()
                continue
            if token.value in {"implementation", "initialization", "finalization"}:
                reject_active_frames()
                continue
            if (
                token.value in _STRUCTURED_TYPE_WORDS
                and _is_structured_type_opener(tokens, index)
            ):
                if index + 1 < len(tokens) and tokens[index + 1].value == ";":
                    frame[1] = index + 2
                    continue
                structured_end = _match_end_terminated_block(tokens, index)
                if structured_end is None:
                    reject_active_frames()
                    continue
                frame[1] = bisect_left(token_starts, structured_end)
                continue
            if token.value == "end":
                reject_active_frames()
                continue
            if token.value in {"begin", "asm"}:
                end = _match_end_terminated_block(tokens, index)
                if end is None:
                    reject_active_frames()
                    continue
                end_index = bisect_left(token_starts, end)
                spans[frame_routine_index] = (
                    tokens[frame_routine_index].start,
                    end,
                    end_index,
                )
                frames.pop()
                continue
            if token.value in _ROUTINE_WORDS and _is_nested_routine_declaration(tokens, index):
                nested_routine_index = _routine_keyword_index(tokens, index)
                if nested_routine_index is None:
                    frame[1] = index + 1
                    continue
                nested = spans.get(nested_routine_index, missing)
                if nested is missing:
                    nested_heading_end = _heading_semicolon_index(tokens, nested_routine_index)
                    if nested_heading_end is None:
                        spans[nested_routine_index] = None
                        continue
                    frames.append([nested_routine_index, nested_heading_end + 1])
                    continue
                if nested is not None:
                    frame[1] = max(index + 1, nested[2])
                    continue
                skipped = _routine_declaration_end_index(tokens, index)
                if skipped is not None:
                    frame[1] = skipped + 1
                    continue
        frame[1] = index + 1

    result = spans.get(routine_index)
    if result is None:
        return None
    return tokens[start_index].start, result[1], result[2]


def _routine_keyword_index(
    tokens: tuple[_Token, ...],
    start_index: int,
) -> int | None:
    for index in range(start_index, min(len(tokens), start_index + 6)):
        token = tokens[index]
        if token.word and not token.escaped and token.value in _ROUTINE_WORDS:
            return index
        if token.value == ";":
            return None
    return None


def _heading_semicolon_index(
    tokens: tuple[_Token, ...],
    routine_index: int,
) -> int | None:
    return _top_level_semicolon_index(tokens, routine_index + 1)


def _routine_declaration_end_index(
    tokens: tuple[_Token, ...],
    routine_index: int,
) -> int | None:
    declaration_end = _heading_semicolon_index(tokens, routine_index)
    if declaration_end is None:
        return None
    cursor = declaration_end + 1
    while cursor < len(tokens):
        directive = tokens[cursor]
        if (
            not directive.word
            or directive.escaped
            or directive.value not in _ROUTINE_DIRECTIVES
        ):
            break
        directive_end = _top_level_semicolon_index(tokens, cursor + 1)
        if directive_end is None:
            break
        declaration_end = directive_end
        cursor = directive_end + 1
    return declaration_end


def _top_level_semicolon_index(
    tokens: tuple[_Token, ...],
    start_index: int,
) -> int | None:
    parentheses = 0
    brackets = 0
    angles = 0
    for index in range(start_index, len(tokens)):
        value = tokens[index].value
        if value == "(":
            parentheses += 1
        elif value == ")":
            parentheses = max(0, parentheses - 1)
        elif value == "[":
            brackets += 1
        elif value == "]":
            brackets = max(0, brackets - 1)
        elif value == "<":
            angles += 1
        elif value == ">":
            angles = max(0, angles - 1)
        elif value == ";" and parentheses == 0 and brackets == 0 and angles == 0:
            return index
    return None


def _is_nested_routine_declaration(
    tokens: tuple[_Token, ...],
    index: int,
) -> bool:
    token = tokens[index]
    if token.value == "operator":
        return True
    previous = tokens[index - 1] if index > 0 else None
    if previous is not None and previous.value in {":", "=", "of", "to", "reference", "."}:
        return False
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if following is None or not following.word or following.value in {"of", "object"}:
        return False
    return True


def _match_end_terminated_block(
    tokens: tuple[_Token, ...],
    opener_index: int,
) -> int | None:
    stack = [tokens[opener_index].value]
    for index in range(opener_index + 1, len(tokens)):
        token = tokens[index]
        if token.directive:
            return None
        if not token.word or token.escaped:
            continue
        value = token.value
        if stack[-1] == "asm":
            if value != "end":
                continue
        elif value in _BLOCK_WORDS:
            if value == "case" and stack[-1] in _STRUCTURED_TYPE_WORDS:
                continue
            stack.append(value)
            continue
        elif value in _STRUCTURED_TYPE_WORDS and _is_structured_type_opener(tokens, index):
            stack.append(value)
            continue
        elif value != "end":
            continue

        stack.pop()
        if stack:
            continue
        end = token.end
        if index + 1 < len(tokens) and tokens[index + 1].value in {";", "."}:
            end = tokens[index + 1].end
        return end
    return None


def _is_structured_type_opener(tokens: tuple[_Token, ...], index: int) -> bool:
    token = tokens[index]
    previous = tokens[index - 1] if index > 0 else None
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if token.value == "class" and following is not None and following.value == "of":
        return False
    if previous is not None and previous.value == "of":
        return token.value in {"record", "object"} and _is_array_of_context(tokens, index)
    if previous is not None and previous.value == ":" and _inside_generic_angles(tokens, index):
        return False
    if previous is None:
        return False
    if previous.value in {"=", ":", "packed"}:
        return True
    if previous.value == "^" and token.value in {"record", "object"}:
        return True
    return False


def _is_array_of_context(tokens: tuple[_Token, ...], index: int) -> bool:
    for candidate in range(index - 2, max(-1, index - 200), -1):
        token = tokens[candidate]
        if token.value == "array":
            return True
        if token.value in {"procedure", "function", "reference", "=", ";"}:
            return False
    return False


def _inside_generic_angles(tokens: tuple[_Token, ...], index: int) -> bool:
    depth = 0
    for candidate in range(index - 1, max(-1, index - 100), -1):
        value = tokens[candidate].value
        if value == ">":
            depth += 1
        elif value == "<":
            if depth == 0:
                return True
            depth -= 1
        elif depth == 0 and value in {";", "begin", "end"}:
            return False
    return False


def _source_items(
    document: _SourceDocument,
    start: int,
    end: int,
    max_chars: int,
    *,
    role: str,
    target_id: str,
) -> list[dict[str, object]]:
    start = min(max(start, 0), len(document.text))
    end = min(max(end, start), len(document.text))
    probe_end = min(end, start + 1)
    compact = max_chars <= 256 or len(
        _compact_json(
            _source_item(
                document,
                start,
                probe_end,
                role=role,
                target_id=target_id,
                chunk_index=999999,
                chunk_count=999999,
                compact=False,
            )
        )
    ) + 2 >= max_chars
    spans: list[tuple[int, int]] = []
    offset = start
    while offset < end:
        upper = min(end, offset + _SOURCE_CHUNK_CHARS)
        accepted = _fit_source_end(
            document,
            offset,
            upper,
            max_chars,
            role=role,
            target_id=target_id,
            compact=compact,
        )
        if accepted <= offset:
            raise AgentProtocolError(
                "item_too_large",
                "max_chars is too small for a typed source chunk.",
            )
        spans.append((offset, accepted))
        offset = accepted
    if not spans:
        spans.append((start, end))

    total = len(spans)
    return [
        _source_item(
            document,
            item_start,
            item_end,
            role=role,
            target_id=target_id,
            chunk_index=index,
            chunk_count=total,
            compact=compact,
        )
        for index, (item_start, item_end) in enumerate(spans)
    ]


def _fit_source_end(
    document: _SourceDocument,
    start: int,
    upper: int,
    max_chars: int,
    *,
    role: str,
    target_id: str,
    compact: bool,
) -> int:
    low = start + 1
    high = upper
    accepted = start
    while low <= high:
        candidate_end = (low + high) // 2
        candidate = _source_item(
            document,
            start,
            candidate_end,
            role=role,
            target_id=target_id,
            chunk_index=999999,
            chunk_count=999999,
            compact=compact,
        )
        if len(_compact_json(candidate)) + 2 <= max_chars:
            accepted = candidate_end
            low = candidate_end + 1
        else:
            high = candidate_end - 1
    return accepted


def _source_item(
    document: _SourceDocument,
    start: int,
    end: int,
    *,
    role: str,
    target_id: str,
    chunk_index: int,
    chunk_count: int,
    compact: bool,
) -> dict[str, object]:
    start_line, start_col = document.line_col(start)
    end_line, end_col = document.line_col(end)
    item: dict[str, object] = {
        "item_type": "source_chunk" if compact else "source",
        "path": document.display_path,
        "start_line": start_line,
        "start_col": start_col,
        "end_line": end_line,
        "end_col": end_col,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "text": document.text[start:end],
    }
    if not compact:
        item["role"] = role
        item["target_id"] = target_id
    return item


def _line_starts(text: str) -> tuple[int, ...]:
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")
    return tuple(starts)


def _lex_delphi(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if character == "{":
            close = text.find("}", index + 1)
            end = length if close < 0 else close + 1
            if index + 1 < length and text[index + 1] == "$":
                tokens.append(
                    _Token(
                        unicodedata.normalize("NFC", text[index:end]),
                        index,
                        end,
                        directive=True,
                    )
                )
            index = end
            continue
        if text.startswith("(*", index):
            close = text.find("*)", index + 2)
            end = length if close < 0 else close + 2
            if index + 2 < length and text[index + 2] == "$":
                tokens.append(
                    _Token(
                        unicodedata.normalize("NFC", text[index:end]),
                        index,
                        end,
                        directive=True,
                    )
                )
            index = end
            continue
        if character == "'":
            block_end = multiline_string_block_end(text, index)
            index = block_end if block_end is not None else _quoted_end(text, index)
            continue
        if character == "&" and index + 1 < length and _identifier_start(text[index + 1]):
            end = index + 2
            while end < length and _identifier_part(text[end]):
                end += 1
            tokens.append(_Token(text[index + 1:end].casefold(), index, end, word=True, escaped=True))
            index = end
            continue
        if _identifier_start(character):
            end = index + 1
            while end < length and _identifier_part(text[end]):
                end += 1
            tokens.append(_Token(text[index:end].casefold(), index, end, word=True))
            index = end
            continue
        if text[index:index + 2] in {":=", "<=", ">=", "<>", ".."}:
            tokens.append(_Token(text[index:index + 2], index, index + 2))
            index += 2
            continue
        tokens.append(_Token(character, index, index + 1))
        index += 1
    return tokens


def _quoted_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] != "'":
            index += 1
            continue
        if index + 1 < len(text) and text[index + 1] == "'":
            index += 2
            continue
        return index + 1
    return len(text)


def _identifier_start(character: str) -> bool:
    return character == "_" or character.isalpha()


def _identifier_part(character: str) -> bool:
    return character == "_" or character.isalnum()


def _ranked_entries(entries: tuple[_SymbolEntry, ...], query: str) -> list[_SymbolEntry]:
    normalized_query = _normalized(query.strip())
    ranked: list[tuple[int, tuple[object, ...], _SymbolEntry]] = []
    for entry in entries:
        normalized_qualified = entry.normalized_qualified_name
        relative_offset = entry.relative_name_offset
        if not normalized_query:
            rank = 3
        elif (
            entry.normalized_name == normalized_query
            or normalized_qualified == normalized_query
            or (
                len(normalized_qualified) - relative_offset == len(normalized_query)
                and normalized_qualified.endswith(normalized_query)
            )
        ):
            rank = 0
        elif (
            entry.normalized_name.startswith(normalized_query)
            or normalized_qualified.startswith(normalized_query)
            or normalized_qualified.startswith(normalized_query, relative_offset)
        ):
            rank = 1
        elif (
            normalized_query in entry.normalized_name
            or normalized_query in normalized_qualified
        ):
            rank = 2
        else:
            continue
        ranked.append((rank, _entry_sort_key(entry), entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def _symbol_sort_key(symbol: Symbol) -> tuple[object, ...]:
    return (
        symbol.decl_range.start_line,
        symbol.decl_range.start_col,
        symbol.kind.value.casefold(),
        _normalized(symbol.name),
        symbol.name,
    )


def _raw_sort_key(raw: _RawSymbol) -> tuple[object, ...]:
    return (
        raw.path.casefold(),
        raw.path,
        raw.line,
        raw.column,
        raw.kind.value.casefold(),
        _normalized(raw.qualified_name),
        raw.qualified_name,
    )


def _entry_sort_key(entry: _SymbolEntry) -> tuple[object, ...]:
    return (
        entry.normalized_qualified_name,
        entry.kind.value.casefold(),
        entry.path.casefold(),
        entry.path,
        entry.line,
        entry.column,
        entry.ordinal,
        entry.target_id,
    )


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _normalized_search_fields(
    name: str,
    qualified_name: str,
    unit_name: str,
) -> tuple[str, str, int]:
    normalized_name = _normalized(name)
    normalized_qualified = _normalized(qualified_name)
    prefix = f"{_normalized(unit_name)}."
    relative_name_offset = (
        len(prefix)
        if normalized_qualified.startswith(prefix)
        else 0
    )
    return normalized_name, normalized_qualified, relative_name_offset


def _request_fingerprint(
    request: AgentRequest,
    *,
    project_id: str,
    target_id: str,
) -> str:
    payload = {
        "action": request.action,
        "depth": request.depth,
        "detail": request.detail,
        "direction": request.direction,
        "graph": request.graph,
        "max_chars": request.max_chars,
        "max_items": request.max_items,
        "project_id": project_id,
        "query": request.query,
        "relation": request.relation,
        "target_id": target_id,
    }
    encoded = _compact_json(payload).encode("utf-8")
    return f"agent_request_v3_{hashlib.sha256(encoded).hexdigest()}"


def _prepare_items(
    items: Sequence[dict[str, object]],
    max_chars: int,
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for item in items:
        if len(_compact_json(item)) + 2 <= max_chars:
            prepared.append(item)
            continue
        prepared.extend(_json_chunks(item, max_chars))
    return prepared


def _json_chunks(item: dict[str, object], max_chars: int) -> list[dict[str, object]]:
    serialized = _compact_json(item)
    if item.get("item_type") in {"source", "source_chunk"}:
        item_type = "source_chunk"
    else:
        item_type = "card_chunk" if "target_id" in item else "json_chunk"
    chunks: list[str] = []
    offset = 0
    while offset < len(serialized):
        low = 1
        high = len(serialized) - offset
        accepted = 0
        while low <= high:
            size = (low + high) // 2
            candidate = {
                "item_type": item_type,
                "chunk_index": len(chunks),
                "chunk_count": 999999,
                "json": serialized[offset:offset + size],
            }
            if len(_compact_json(candidate)) + 2 <= max_chars:
                accepted = size
                low = size + 1
            else:
                high = size - 1
        if accepted == 0:
            raise AgentProtocolError(
                "item_too_large",
                "max_chars is too small for a structured response chunk.",
            )
        chunks.append(serialized[offset:offset + accepted])
        offset += accepted
    total = len(chunks)
    return [
        {
            "item_type": item_type,
            "chunk_index": index,
            "chunk_count": total,
            "json": chunk,
        }
        for index, chunk in enumerate(chunks)
    ]


def _compact_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise AgentProtocolError("invalid_item", "Item is not JSON-compatible.") from None


__all__ = ["AgentContext"]
