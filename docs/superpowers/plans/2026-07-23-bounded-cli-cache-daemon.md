# Bounded CLI Cache Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded per-workspace background daemon that prewarms the existing Delphi semantic navigator and serves later `delphi-lsp-agent query` processes from memory.

**Architecture:** A new `agent_cache` module owns metadata, authenticated loopback transport, cache accounting, eviction, and daemon lifecycle. `AgentContext` and `AgentWorkspace` expose narrow cache-observation and eviction methods without changing Protocol v2 behavior. `agent_cli` translates ergonomic query subcommands into existing Protocol v2 requests and keeps JSON output separate from utilization warnings.

**Tech Stack:** Python 3.10+, standard-library `argparse`, `socket`, `socketserver`, `subprocess`, `threading`, `secrets`, `json`, existing Protocol v2 and pytest suite.

---

## File Structure

- Create `delphi_lsp/agent_cache.py`: daemon metadata, deep-size estimation, budget decisions, authenticated server/client transport, lifecycle, and status.
- Modify `delphi_lsp/agent_workspace.py`: expose owned cache roots and clear recomputable project-index state.
- Modify `delphi_lsp/agent_context.py`: expose cache state, auxiliary eviction, and full navigation eviction.
- Modify `delphi_lsp/agent_cli.py`: add `cache start/status/stop/serve` and `query` commands.
- Create `tests/test_agent_cache.py`: deterministic unit tests for accounting, warning, metadata, authentication, eviction, and lifecycle.
- Modify `tests/test_agent_worker.py`: parser and separate-process query regressions.
- Modify `tests/test_agent_codebase.py`: preserve generated OpenCode worker reuse.
- Modify `.gitignore`: ignore `.delphi-lsp/` runtime metadata.
- Modify `README.md`: document lifecycle, memory semantics, warning behavior, and OpenCode version history.

### Task 1: Expose Bounded Cache State

**Files:**
- Modify: `delphi_lsp/agent_workspace.py`
- Modify: `delphi_lsp/agent_context.py`
- Create: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing tests for cache roots and eviction**

Add:

```python
from pathlib import Path

from delphi_lsp.agent_cache import estimate_deep_size
from delphi_lsp.agent_context import AgentContext


def _write_project(root: Path) -> None:
    (root / "Main.dpr").write_text(
        "program Main; uses UnitA in 'UnitA.pas'; begin end.\n",
        encoding="utf-8",
    )
    (root / "UnitA.pas").write_text(
        "unit UnitA; interface type TCustomer = class end; implementation end.\n",
        encoding="utf-8",
    )


def test_context_reports_and_evicts_recomputable_cache(tmp_path: Path) -> None:
    _write_project(tmp_path)
    context = AgentContext.open(tmp_path)
    context.handle({"action": "find", "query": "TCustomer"})

    assert context.navigation_cache_is_warm is True
    assert estimate_deep_size(context.cache_roots()) > 0

    context.evict_auxiliary_caches()
    assert context.navigation_cache_is_warm is True

    context.evict_navigation_caches()
    assert context.navigation_cache_is_warm is False
    assert context.workspace.active_project_id

    rebuilt = context.handle({"action": "find", "query": "TCustomer"})
    assert rebuilt.result[0]["name"] == "TCustomer"
    assert context.navigation_cache_is_warm is True
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py::test_context_reports_and_evicts_recomputable_cache -v
```

Expected: collection fails because `delphi_lsp.agent_cache` and the cache-control API do not exist.

- [ ] **Step 3: Add narrow cache-control methods**

Add to `AgentWorkspace`:

```python
    def cache_roots(self) -> tuple[object, ...]:
        return (self._project_cache, self._active_result)

    def evict_recomputable_caches(self) -> None:
        self._project_cache.clear()
        self._active_result = None
```

Add to `AgentContext`:

```python
    @property
    def navigation_cache_is_warm(self) -> bool:
        return self._registry is not None

    def cache_roots(self) -> tuple[object, ...]:
        return (
            self._workspace.cache_roots(),
            self._registry,
            self._relation_index,
            self._metrics,
        )

    def evict_auxiliary_caches(self) -> None:
        self._relation_index = None
        self._metrics = None
        self._metrics_revision = ""

    def evict_navigation_caches(self) -> None:
        self.evict_auxiliary_caches()
        self._registry = None
        self._workspace.evict_recomputable_caches()
```

Create the initial memory-accounting portion of `agent_cache.py`:

```python
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import sys
from types import ModuleType


def estimate_deep_size(value: object) -> int:
    seen: set[int] = set()
    pending: deque[object] = deque([value])
    total = 0
    while pending:
        current = pending.popleft()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, (ModuleType, type)) or callable(current):
            continue
        total += sys.getsizeof(current)
        if isinstance(current, Mapping):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (tuple, list, set, frozenset, deque)):
            pending.extend(current)
        elif is_dataclass(current) and not isinstance(current, type):
            pending.extend(getattr(current, field.name) for field in fields(current))
        elif hasattr(current, "__dict__"):
            pending.append(vars(current))
        elif hasattr(current, "__slots__"):
            slots = current.__slots__
            for name in (slots,) if isinstance(slots, str) else slots:
                if hasattr(current, name):
                    pending.append(getattr(current, name))
    return total
```

- [ ] **Step 4: Verify GREEN and run context regressions**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py::test_context_reports_and_evicts_recomputable_cache tests/test_agent_context.py tests/test_agent_workspace.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add delphi_lsp/agent_cache.py delphi_lsp/agent_context.py delphi_lsp/agent_workspace.py tests/test_agent_cache.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/check_staged.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/commit.py \
  --title "Expose semantic cache eviction controls" \
  --bullet "Measure only daemon-owned recomputable object graphs" \
  --bullet "Rebuild navigation correctly after compact eviction"
```

### Task 2: Implement Budget Decisions and 80-Percent Warnings

**Files:**
- Modify: `delphi_lsp/agent_cache.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing tests for size parsing, warning, and eviction**

Add:

```python
import pytest

from delphi_lsp.agent_cache import (
    CacheBudget,
    CacheStats,
    parse_memory_size,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512M", 512 * 1024**2),
        ("1G", 1024**3),
        ("4096K", 4096 * 1024),
        ("1048576", 1048576),
    ],
)
def test_parse_memory_size(text: str, expected: int) -> None:
    assert parse_memory_size(text) == expected


def test_warning_threshold_is_inclusive_and_eviction_is_ordered() -> None:
    calls: list[str] = []
    sizes = iter([80, 101, 90, 20])
    budget = CacheBudget(max_bytes=100, warning_percent=80)

    first = budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert first.warning_active is True
    assert first.utilization_percent == 80.0
    assert calls == []

    second = budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert calls == ["auxiliary", "navigation"]
    assert second.compacted is True
    assert second.warning_triggered is True
    assert second.warning_active is False
    assert second.retained_bytes == 20
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py -k 'memory_size or warning_threshold' -v
```

Expected: import failures for `CacheBudget`, `CacheStats`, and `parse_memory_size`.

- [ ] **Step 3: Implement the budget model**

Add these public types and functions:

```python
from dataclasses import dataclass
import re
from typing import Callable


DEFAULT_MAX_MEMORY_BYTES = 512 * 1024**2
WARNING_THRESHOLD_PERCENT = 80
_MEMORY_SIZE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<suffix>[KMG]?)$", re.IGNORECASE)


def parse_memory_size(value: str) -> int:
    match = _MEMORY_SIZE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Memory size must be a positive integer with optional K, M, or G suffix.")
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
    return int(match.group("count")) * multiplier[match.group("suffix").upper()]


@dataclass
class CacheStats:
    requests: int = 0
    warm_hits: int = 0
    rebuilds: int = 0
    invalidations: int = 0
    evictions: int = 0


@dataclass(frozen=True)
class BudgetResult:
    retained_bytes: int
    utilization_percent: float
    peak_utilization_percent: float
    warning_active: bool
    warning_triggered: bool
    compacted: bool


@dataclass(frozen=True)
class CacheBudget:
    max_bytes: int = DEFAULT_MAX_MEMORY_BYTES
    warning_percent: int = WARNING_THRESHOLD_PERCENT

    def enforce(
        self,
        *,
        measure: Callable[[], int],
        evict_auxiliary: Callable[[], None],
        evict_navigation: Callable[[], None],
    ) -> BudgetResult:
        initial = measure()
        initial_percent = initial * 100.0 / self.max_bytes
        compacted = False
        retained = initial
        if initial > self.max_bytes:
            evict_auxiliary()
            compacted = True
            retained = measure()
            if retained > self.max_bytes:
                evict_navigation()
                retained = measure()
        retained_percent = retained * 100.0 / self.max_bytes
        return BudgetResult(
            retained_bytes=retained,
            utilization_percent=retained_percent,
            peak_utilization_percent=initial_percent,
            warning_active=retained_percent >= self.warning_percent,
            warning_triggered=initial_percent >= self.warning_percent,
            compacted=compacted,
        )
```

Implement a formatter that does not expose the authentication token:

```python
def cache_warning(result: BudgetResult, max_bytes: int) -> str:
    if not result.warning_triggered:
        return ""
    action = " Cache was compacted." if result.compacted else ""
    return (
        "Warning: Delphi cache reached "
        f"{result.peak_utilization_percent:.1f}% of {max_bytes} bytes.{action} "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py -k 'memory_size or warning_threshold' -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add delphi_lsp/agent_cache.py tests/test_agent_cache.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/check_staged.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/commit.py \
  --title "Bound retained Delphi navigation caches" \
  --bullet "Evict auxiliary state before the semantic registry" \
  --bullet "Warn at the inclusive eighty-percent threshold"
```

### Task 3: Add Authenticated Daemon Lifecycle

**Files:**
- Modify: `delphi_lsp/agent_cache.py`
- Modify: `tests/test_agent_cache.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing metadata and transport tests**

Add tests that use a real temporary workspace and always stop the daemon:

```python
import json
import socket
import time

from delphi_lsp.agent_cache import (
    CacheClientError,
    cache_metadata_path,
    cache_status,
    query_cache,
    start_cache,
    stop_cache,
)


def test_daemon_reuses_one_pid_and_rejects_bad_token(tmp_path: Path) -> None:
    _write_project(tmp_path)
    started = start_cache(tmp_path, max_memory_bytes=512 * 1024**2, idle_timeout=60)
    try:
        first = query_cache(tmp_path, {"action": "find", "query": "TCustomer"})
        second = query_cache(tmp_path, {"action": "open"})
        status = cache_status(tmp_path)

        assert first.payload["result"][0]["name"] == "TCustomer"
        assert second.payload["schema"] == 2
        assert status["pid"] == started["pid"]
        assert status["requests"] >= 2
        assert status["cache_state"] == "warm"

        metadata_path = cache_metadata_path(tmp_path)
        original_metadata = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(original_metadata)
        metadata["token"] = "invalid"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(CacheClientError, match="authentication"):
            query_cache(tmp_path, {"action": "open"})
        metadata_path.write_text(original_metadata, encoding="utf-8")
    finally:
        stop_cache(tmp_path)


def test_tiny_budget_compacts_but_later_queries_still_work(tmp_path: Path) -> None:
    _write_project(tmp_path)
    start_cache(tmp_path, max_memory_bytes=1, idle_timeout=60)
    try:
        response = query_cache(tmp_path, {"action": "find", "query": "TCustomer"})
        status = cache_status(tmp_path)
        assert response.payload["result"][0]["name"] == "TCustomer"
        assert response.warning
        assert status["cache_state"] == "compact"
        assert status["evictions"] >= 1
        assert query_cache(tmp_path, {"action": "find", "query": "TCustomer"}).payload["result"]
    finally:
        stop_cache(tmp_path)
```

- [ ] **Step 2: Run transport tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py -k 'daemon_reuses or tiny_budget' -v
```

Expected: import failures for daemon lifecycle functions.

- [ ] **Step 3: Implement metadata and one-request transport**

Add:

```python
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import threading
import time

from ._version import __version__
from .agent_context import AgentContext
from .agent_protocol import AgentProtocolError


DAEMON_SCHEMA = 1
DEFAULT_IDLE_TIMEOUT = 1800


@dataclass(frozen=True)
class CacheMetadata:
    schema: int
    root: str
    pid: int
    port: int
    token: str
    version: str
    project_file: str
    max_memory_bytes: int
    idle_timeout: int
    started_at: float


@dataclass(frozen=True)
class CacheClientResponse:
    payload: dict[str, object]
    warning: str = ""


class CacheClientError(RuntimeError):
    pass


def cache_metadata_path(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / ".delphi-lsp" / "agent-cache" / "daemon.json"
```

Implement atomic metadata writes with a temporary sibling, `os.replace`, mode
`0o600`, and parent mode `0o700` on POSIX. Validate schema, canonical root,
integer port/PID, and nonempty token on every client load.

Implement `_CacheService` with these exact responsibilities:

```python
class _CacheService:
    def __init__(self, context: AgentContext, budget: CacheBudget, idle_timeout: int) -> None:
        self.context = context
        self.budget = budget
        self.idle_timeout = idle_timeout
        self.started_at = time.time()
        self.last_activity = time.monotonic()
        self.stats = CacheStats()
        self.lock = threading.Lock()
        self.cache_state = "warming"
        self.last_budget = budget.enforce(
            measure=lambda: estimate_deep_size(context.cache_roots()),
            evict_auxiliary=context.evict_auxiliary_caches,
            evict_navigation=context.evict_navigation_caches,
        )

    def query(self, request: dict[str, object]) -> CacheClientResponse:
        with self.lock:
            self.last_activity = time.monotonic()
            was_warm = self.context.navigation_cache_is_warm
            previous_revision = self.context.workspace.workspace_revision
            response = self.context.handle(request).to_mapping()
            current_revision = str(response["workspace_revision"])
            self.stats.requests += 1
            self.stats.warm_hits += int(was_warm)
            self.stats.rebuilds += int(not was_warm and self.context.navigation_cache_is_warm)
            self.stats.invalidations += int(current_revision != previous_revision)
            self.last_budget = self.budget.enforce(
                measure=lambda: estimate_deep_size(self.context.cache_roots()),
                evict_auxiliary=self.context.evict_auxiliary_caches,
                evict_navigation=self.context.evict_navigation_caches,
            )
            if self.last_budget.compacted:
                self.stats.evictions += 1
            self.cache_state = "warm" if self.context.navigation_cache_is_warm else "compact"
            return CacheClientResponse(
                payload=response,
                warning=cache_warning(self.last_budget, self.budget.max_bytes),
            )
```

The server binds `("127.0.0.1", 0)`, compares request tokens with
`secrets.compare_digest`, rejects records over 1 MiB, serializes protocol
exceptions with the worker's sanitized error policy, and processes exactly one
request per connection. Internal operations are `status` and `stop`; all other
payloads contain a Protocol v2 `request`.

`run_cache_daemon` must:

1. create `AgentContext`;
2. prewarm with `find` using `max_items=1`, `max_chars=256`;
3. bind before publishing atomic metadata;
4. call `handle_request()` with a one-second server timeout;
5. stop at `idle_timeout`;
6. unlink metadata only if its token and PID still match.

`start_cache` spawns:

```python
[
    sys.executable,
    "-m",
    "delphi_lsp.agent_cli",
    "cache",
    "serve",
    "--root",
    str(root),
    "--max-memory",
    str(max_memory_bytes),
    "--idle-timeout",
    str(idle_timeout),
]
```

Use `stdin/stdout/stderr=subprocess.DEVNULL`, `start_new_session=True` on
POSIX, and `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows. Poll
authenticated status for at most ten seconds. Reuse a live daemon at the same
root only when project, memory budget, and idle timeout match; report a
configuration conflict otherwise. Remove stale metadata and terminate the
child if readiness fails.

Add `.delphi-lsp/` to `.gitignore`.

- [ ] **Step 4: Verify daemon GREEN and no orphan**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py -k 'daemon or budget or metadata or authentication' -v
pgrep -f 'delphi_lsp.agent_cli cache serve' || true
```

Expected: selected tests pass and no test daemon remains.

- [ ] **Step 5: Commit**

```bash
git add .gitignore delphi_lsp/agent_cache.py tests/test_agent_cache.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/check_staged.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/commit.py \
  --title "Add authenticated Delphi cache daemon" \
  --bullet "Prewarm one semantic context per canonical workspace root" \
  --bullet "Clean stale metadata and stop idle or explicit daemon lifecycles"
```

### Task 4: Add CLI Commands and Separate-Process Integration

**Files:**
- Modify: `delphi_lsp/agent_cli.py`
- Modify: `tests/test_agent_worker.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing parser and CLI integration tests**

Extend the parser test:

```python
    cache_start = parser.parse_args(
        ["cache", "start", "--root", "workspace", "--max-memory", "768M"]
    )
    query = parser.parse_args(
        ["query", "--root", "workspace", "inspect", "target_v2_abc", "--detail", "body"]
    )
    assert cache_start.cache_command == "start"
    assert cache_start.max_memory == 768 * 1024**2
    assert query.action == "inspect"
    assert query.value == "target_v2_abc"
    assert query.detail == "body"
```

Add a subprocess integration test:

```python
def test_separate_cli_queries_share_daemon_and_warn_on_stderr(tmp_path: Path) -> None:
    _write_project(tmp_path)
    base = [sys.executable, "-m", "delphi_lsp.agent_cli"]
    started = subprocess.run(
        base + ["cache", "start", "--root", str(tmp_path), "--max-memory", "1"],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        first = subprocess.run(
            base + ["query", "--root", str(tmp_path), "find", "TCustomer"],
            text=True,
            capture_output=True,
            check=False,
        )
        status = subprocess.run(
            base + ["cache", "status", "--root", str(tmp_path), "--format", "json"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert started.returncode == 0
        assert first.returncode == 0
        assert json.loads(first.stdout)["result"][0]["name"] == "TCustomer"
        assert "Warning: Delphi cache reached" in first.stderr
        status_payload = json.loads(status.stdout)
        assert status_payload["pid"] == json.loads(started.stdout)["pid"]
        assert status_payload["warning_threshold_percent"] == 80
    finally:
        subprocess.run(
            base + ["cache", "stop", "--root", str(tmp_path)],
            capture_output=True,
            check=False,
        )
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_agent_worker.py::test_parser_adds_worker_without_changing_legacy_commands tests/test_agent_cache.py::test_separate_cli_queries_share_daemon_and_warn_on_stderr -v
```

Expected: argparse rejects `cache` and `query`.

- [ ] **Step 3: Implement the CLI surface**

Import `SUPPORTED_ACTIONS`, `SUPPORTED_DETAILS`, and `SUPPORTED_RELATIONS` from
`agent_protocol`, plus lifecycle functions from `agent_cache`.

Create subparsers:

```python
    cache = subcommands.add_parser("cache", help="Manage the shared semantic cache.")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)

    cache_start = cache_commands.add_parser("start", help="Start and prewarm a cache daemon.")
    cache_start.add_argument("--root", type=Path, default=Path("."))
    cache_start.add_argument("--project-file", type=Path)
    cache_start.add_argument("--max-memory", type=parse_memory_size, default=DEFAULT_MAX_MEMORY_BYTES)
    cache_start.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT)
    cache_start.set_defaults(func=_cache_start)

    cache_status_parser = cache_commands.add_parser("status", help="Show daemon cache status.")
    cache_status_parser.add_argument("--root", type=Path, default=Path("."))
    cache_status_parser.add_argument("--format", choices=["text", "json"], default="text")
    cache_status_parser.set_defaults(func=_cache_status)

    cache_stop_parser = cache_commands.add_parser("stop", help="Stop the cache daemon.")
    cache_stop_parser.add_argument("--root", type=Path, default=Path("."))
    cache_stop_parser.set_defaults(func=_cache_stop)

    cache_serve = cache_commands.add_parser("serve", help=argparse.SUPPRESS)
    cache_serve.add_argument("--root", type=Path, required=True)
    cache_serve.add_argument("--project-file", type=Path)
    cache_serve.add_argument("--max-memory", type=parse_memory_size, required=True)
    cache_serve.add_argument("--idle-timeout", type=int, required=True)
    cache_serve.set_defaults(func=_cache_serve)

    query = subcommands.add_parser("query", help="Query a running semantic cache.")
    query.add_argument("--root", type=Path, default=Path("."))
    query.add_argument("action", choices=SUPPORTED_ACTIONS)
    query.add_argument("value", nargs="?", default="")
    query.add_argument("--project-id", default="")
    query.add_argument("--detail", choices=SUPPORTED_DETAILS, default="summary")
    query.add_argument("--relation", choices=SUPPORTED_RELATIONS)
    query.add_argument("--cursor", default="")
    query.add_argument("--max-items", type=int, default=12)
    query.add_argument("--max-chars", type=int, default=12000)
    query.set_defaults(func=_query)
```

Translate positional values exactly:

```python
def _query_request(args: argparse.Namespace) -> dict[str, object]:
    request: dict[str, object] = {
        "action": args.action,
        "project_id": args.project_id,
        "detail": args.detail,
        "relation": args.relation,
        "cursor": args.cursor,
        "max_items": args.max_items,
        "max_chars": args.max_chars,
    }
    if args.action in {"find", "metrics"}:
        request["query"] = args.value
    elif args.action in {"focus", "inspect", "trace"}:
        request["target_id"] = args.value
    elif args.value:
        raise AgentProtocolError("unexpected_value", f"{args.action} does not accept a value.")
    return request
```

Each handler prints deterministic JSON with `sort_keys=True`; `query` prints
only the Protocol response to stdout and any warning to stderr. Catch
`CacheClientError`, print `cache_not_running` or the sanitized cache error to
stderr, and return a nonzero exit code. Change `main` to preserve current
zero-returning handlers while propagating explicit handler codes:

```python
        result = args.func(args)
        if not getattr(sys.stdout, "closed", False):
            sys.stdout.flush()
        return result if isinstance(result, int) else 0
```

- [ ] **Step 4: Verify CLI GREEN and legacy worker compatibility**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py tests/test_agent_worker.py -q
```

Expected: all tests pass and no daemon remains.

- [ ] **Step 5: Commit**

```bash
git add delphi_lsp/agent_cli.py tests/test_agent_cache.py tests/test_agent_worker.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/check_staged.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/commit.py \
  --title "Expose shared cache lifecycle in CLI" \
  --bullet "Translate ergonomic query commands to Protocol v2 requests" \
  --bullet "Keep warnings on stderr and protocol JSON on stdout"
```

### Task 5: Document, Harden, and Run Release Gates

**Files:**
- Modify: `README.md`
- Modify: `tests/test_agent_codebase.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Add failing documentation and OpenCode regression assertions**

Add assertions that README contains:

```python
assert "delphi-lsp-agent cache start --root PATH" in readme
assert "delphi-lsp-agent query --root PATH find TCustomer" in readme
assert "80 percent" in readme
assert "first released in 2.0.0" in readme
```

Retain the generated-plugin runtime assertions:

```python
assert processes.length == 1
assert processes[0].flushes == 3
```

Add lifecycle edge tests for:

- stale metadata is removed before restart;
- `cache stop` is idempotent;
- source editing changes `workspace_revision` and increments invalidations;
- idle timeout removes metadata;
- status JSON excludes `token`;
- exactly 80 percent warns, while a current value below 80 does not;
- malformed and oversized requests do not terminate the daemon.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py tests/test_agent_codebase.py -k 'readme or cache or generated_plugin_runtime' -v
```

Expected: README assertions and any uncovered lifecycle edge assertions fail.

- [ ] **Step 3: Complete documentation and hardening**

Update the CLI block with `cache` and `query` examples. Explain:

- one daemon owns one canonical root;
- 512 MiB is a retained-cache budget, not a hard RSS cap;
- warning begins at `>=80%`;
- compaction drops relation/metric state before the navigation registry;
- source revisions invalidate stale structures;
- stdout remains Protocol v2 JSON and warnings use stderr;
- OpenCode 1.1.0/1.1.1 spawned one `view` process per call;
- the persistent OpenCode worker first shipped in 2.0.0;
- OpenCode session workers remain separate from the new CLI daemon.

Fix lifecycle behavior until every edge test passes. Do not change the generated
OpenCode plugin to share the CLI daemon in this feature.

- [ ] **Step 4: Run focused and complete verification**

Run:

```bash
.venv/bin/pytest tests/test_agent_cache.py tests/test_agent_worker.py tests/test_agent_codebase.py -q
.venv/bin/pytest -q
.venv/bin/python -m ruff check delphi_lsp tests
.venv/bin/python -m mypy delphi_lsp
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
git diff --check
pgrep -f 'delphi_lsp.agent_cli cache serve' || true
```

Expected:

- focused and full tests pass;
- Ruff, mypy, build, and Twine checks pass;
- `git diff --check` is silent;
- no cache daemon remains;
- wheel and source distribution contain `delphi_lsp/agent_cache.py`.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_agent_cache.py tests/test_agent_codebase.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/check_staged.py
python /Volumes/MacDataSSDPro/.codex/skills/git-commit-policy/scripts/commit.py \
  --title "Document bounded Delphi CLI caching" \
  --bullet "Explain memory warnings, compaction, and daemon lifecycle" \
  --bullet "Preserve and date the independent OpenCode worker cache"
```

### Task 6: Final Integration and Push

**Files:**
- Verify all changed files

- [ ] **Step 1: Inspect the complete branch diff**

Run:

```bash
git status --short
git diff origin/main...HEAD --stat
git diff origin/main...HEAD --check
```

Expected: only scoped cache-daemon, tests, ignore, and documentation changes;
no whitespace errors.

- [ ] **Step 2: Update and close the Beads issue after verification**

From `/Volumes/MacDataSSDPro/mac_ops`:

```bash
bd update mac-1q3y --notes="Implemented and verified bounded shared CLI cache daemon, inclusive 80-percent warnings, compact fallback, lifecycle commands, separate-process queries, and preserved OpenCode worker caching."
bd close mac-1q3y --reason="Implementation, tests, documentation, and release gates passed."
bd dolt push
```

- [ ] **Step 3: Rebase and push**

Run:

```bash
git pull --rebase origin main
git push origin HEAD
git status --short --branch
```

Expected: push succeeds and status reports the branch up to date with its
remote.
