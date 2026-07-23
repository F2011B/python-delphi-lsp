# Parallel Cache Prewarming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse independent Delphi units concurrently during cold cache, worker, view, and index builds while preserving deterministic Protocol v2 output and bounded memory behavior.

**Architecture:** A new `parallel_outline` module owns worker parsing and a short-lived spawn-based process pool. Existing index builders submit immutable unit tasks, merge returned semantic models in canonical order, and expose timing/worker statistics without retaining executor state.

**Tech Stack:** Python 3.10+, `concurrent.futures.ProcessPoolExecutor`, `multiprocessing` spawn context, existing Delphi outline parser, pytest, Ruff.

---

### Task 1: Parallel outline execution core

**Files:**
- Create: `delphi_lsp/parallel_outline.py`
- Create: `tests/test_parallel_outline.py`

- [ ] **Step 1: Write failing worker parsing and selection tests**

```python
from pathlib import Path

import pytest

from delphi_lsp.parallel_outline import (
    OutlineTask,
    parse_worker_setting,
    resolve_worker_count,
    run_outline_tasks,
)


def test_parse_worker_setting_accepts_auto_and_bounded_integers() -> None:
    assert parse_worker_setting("auto") == 0
    assert parse_worker_setting("1") == 1
    assert parse_worker_setting("32") == 32
    with pytest.raises(ValueError):
        parse_worker_setting("0")
    with pytest.raises(ValueError):
        parse_worker_setting("33")


def test_auto_workers_respect_cpu_tasks_and_cache_budget() -> None:
    assert resolve_worker_count(0, task_count=20, cpu_count=8, memory_budget_bytes=512 * 1024**2) == 4
    assert resolve_worker_count(0, task_count=20, cpu_count=8, memory_budget_bytes=128 * 1024**2) == 1
    assert resolve_worker_count(0, task_count=2, cpu_count=8, memory_budget_bytes=None) == 2
    assert resolve_worker_count(7, task_count=3, cpu_count=2, memory_budget_bytes=1) == 3


def test_serial_outline_task_returns_model_text_and_counts(tmp_path: Path) -> None:
    source = tmp_path / "One.pas"
    source.write_text("unit One; interface implementation end.\n", encoding="utf-8")
    batch = run_outline_tasks(
        [OutlineTask(0, str(source), (), True)],
        configured_workers=1,
    )
    assert batch.stats.effective_workers == 1
    assert batch.stats.files_completed == 1
    assert batch.results[0].text.startswith("unit One")
    assert batch.results[0].model.unit_scope.name == "One"
```

- [ ] **Step 2: Run tests and verify the missing-module failure**

Run:

```bash
../../.venv/bin/pytest -q tests/test_parallel_outline.py
```

Expected: collection fails with `ModuleNotFoundError: delphi_lsp.parallel_outline`.

- [ ] **Step 3: Implement immutable tasks, results, statistics, and worker selection**

Create these public package-internal contracts:

```python
@dataclass(frozen=True)
class OutlineTask:
    ordinal: int
    source_path: str
    defines: tuple[str, ...]
    return_text: bool


@dataclass(frozen=True)
class OutlineResult:
    ordinal: int
    source_path: str
    text: str
    model: SemanticModel | None
    lines_processed: int
    symbols_discovered: int
    read_error: str = ""


@dataclass(frozen=True)
class ParallelBuildStats:
    configured_workers: int
    effective_workers: int
    files_completed: int
    elapsed_seconds: float
    fallbacks: int


@dataclass(frozen=True)
class OutlineBatch:
    results: tuple[OutlineResult, ...]
    stats: ParallelBuildStats
```

Implement `parse_worker_setting()` so `auto` maps to zero and explicit values
are limited to 1–32. Implement `resolve_worker_count()` with the approved
task/CPU/four-worker/128-MiB formula. `_parse_outline_task()` must be a top-level
picklable function that calls `read_source_text()` and
`build_outline_semantic_model()`, returns read errors as data, and allows parser
exceptions to propagate.

- [ ] **Step 4: Add failing process-pool, ordering, fallback, and callback tests**

Tests must:

```python
batch = run_outline_tasks(tasks, configured_workers=2)
assert [result.ordinal for result in batch.results] == list(range(len(tasks)))
assert batch.stats.effective_workers == 2
```

Monkeypatch the module-level executor constructor to raise before submission and
assert automatic mode retries serially with `fallbacks == 1`, while explicit
mode raises `ParallelOutlineError`. Use a callback that raises
`RuntimeError("callback failed")` and assert the same exception escapes.

- [ ] **Step 5: Implement spawn-pool execution and deterministic collection**

Use:

```python
context = multiprocessing.get_context("spawn")
with ProcessPoolExecutor(max_workers=effective, mp_context=context) as executor:
    future_to_task = {executor.submit(_parse_outline_task, task): task for task in tasks}
    for future in as_completed(future_to_task):
        result = future.result()
        accepted.append(result)
        if on_complete is not None:
            on_complete(result)
```

Cancel pending futures on any failure. Sort accepted results by `ordinal`
before returning. Fall back once only when configured workers are automatic,
pool startup fails, and no result was accepted.

- [ ] **Step 6: Run the parallel core tests**

Run:

```bash
../../.venv/bin/pytest -q tests/test_parallel_outline.py
PYENV_VERSION=venv3.14.0 pyenv exec ruff check delphi_lsp/parallel_outline.py tests/test_parallel_outline.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 7: Commit the core**

Stage only the new module and its test, run the staged-file policy check, then
commit with title `Add bounded parallel outline execution`.

### Task 2: Layered view and index integration

**Files:**
- Modify: `delphi_lsp/agent_layers.py`
- Modify: `tests/test_progress.py`
- Modify: `tests/test_agent_codebase.py`

- [ ] **Step 1: Write failing serial-versus-parallel equivalence tests**

Build the same multi-unit fixture twice:

```python
serial = build_codebase_index(tmp_path, workers=1)
parallel = build_codebase_index(tmp_path, workers=2)
assert layer_payload(parallel, "symbols") == layer_payload(serial, "symbols")
assert list(parallel.models) == list(serial.models)
assert parallel.parallel_stats.effective_workers == 2
```

Update the progress test to allow completion-order outline events while still
requiring monotonic counters and a deterministic final index.

- [ ] **Step 2: Verify the new keyword and stats fail**

Run:

```bash
../../.venv/bin/pytest -q tests/test_progress.py tests/test_agent_codebase.py
```

Expected: failures report the unsupported `workers` keyword or missing
`parallel_stats`.

- [ ] **Step 3: Replace the serial outline loop**

Add `parallel_stats: ParallelBuildStats` to `CodebaseIndex` and extend:

```python
def build_codebase_index(
    root: str | Path,
    *,
    project_file: str | Path | None = None,
    index_projects: bool = False,
    on_progress: ProgressCallback | None = None,
    workers: int = 0,
) -> CodebaseIndex:
```

Create ordered `OutlineTask` values for supported sources, call
`run_outline_tasks(..., configured_workers=workers)`, skip results containing a
read error, and register successful models only after results return in ordinal
order. Emit completion progress from the parent callback with accumulated line
and symbol counts.

- [ ] **Step 4: Run view/index tests and Ruff**

Run:

```bash
../../.venv/bin/pytest -q tests/test_progress.py tests/test_agent_codebase.py
PYENV_VERSION=venv3.14.0 pyenv exec ruff check delphi_lsp/agent_layers.py tests/test_progress.py tests/test_agent_codebase.py
```

Expected: all selected checks pass.

- [ ] **Step 5: Commit layered integration**

Commit the three scoped files with title
`Parallelize layered Delphi outline builds`.

### Task 3: Protocol v2 registry and OpenCode worker integration

**Files:**
- Modify: `delphi_lsp/agent_context.py`
- Modify: `delphi_lsp/agent_cli.py`
- Modify: `tests/test_agent_context.py`
- Modify: `tests/test_agent_worker.py`

- [ ] **Step 1: Write failing deterministic registry tests**

Create a project containing at least three units and compare:

```python
serial = AgentContext.open(tmp_path, workers=1)
parallel = AgentContext.open(tmp_path, workers=2)
serial_cards = serial.handle({"action": "find", "query": "", "max_items": 50}).to_mapping()["result"]
parallel_cards = parallel.handle({"action": "find", "query": "", "max_items": 50}).to_mapping()["result"]
assert parallel_cards == serial_cards
assert parallel.parallel_stats.effective_workers == 2
```

Add an unreadable-source test that requires `source_unavailable`, plus an
argument parser test requiring `worker --workers 2`.

- [ ] **Step 2: Verify failures**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_context.py tests/test_agent_worker.py
```

Expected: failures identify missing worker configuration and stats.

- [ ] **Step 3: Thread worker configuration through `AgentContext`**

Extend `AgentContext.__init__()` and `AgentContext.open()` with
`workers: int = 0` and `worker_memory_budget_bytes: int | None = None`.
Initialize a zero-valued `ParallelBuildStats` and expose it through:

```python
@property
def parallel_stats(self) -> ParallelBuildStats:
    return self._parallel_stats
```

Change `_build_registry()` to return `tuple[_Registry, ParallelBuildStats]`.
It must call `run_outline_tasks(return_text=True)`, convert read errors to the
existing sanitized `source_unavailable` protocol error, build `_SourceDocument`
instances from returned text, collect raw symbols in ordinal order, and leave
all target construction in the parent.

- [ ] **Step 4: Add worker CLI configuration**

Register `--workers` on `worker` with `type=parse_worker_setting` and pass the
value to `AgentContext.open()`. Default zero keeps OpenCode's generated worker
command automatically parallel without changing its session/root ownership.

- [ ] **Step 5: Run focused tests and Ruff**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_context.py tests/test_agent_worker.py tests/test_agent_relations.py
PYENV_VERSION=venv3.14.0 pyenv exec ruff check delphi_lsp/agent_context.py delphi_lsp/agent_cli.py tests/test_agent_context.py tests/test_agent_worker.py
```

Expected: all selected checks pass.

- [ ] **Step 6: Commit Protocol v2 integration**

Commit with title `Parallelize Protocol v2 registry prewarming`.

### Task 4: Cache configuration, status, and startup timeout

**Files:**
- Modify: `delphi_lsp/agent_cache.py`
- Modify: `delphi_lsp/agent_cli.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing metadata and status tests**

Update all `CacheMetadata` fixtures and add assertions:

```python
metadata = start_cache(
    tmp_path,
    max_memory_bytes=512 * 1024**2,
    workers=2,
    startup_timeout=30,
)
status = cache_status(tmp_path)
assert status["workers_configured"] == 2
assert status["workers_effective"] == 2
assert status["parallel_files_completed"] >= 2
assert status["prewarm_seconds"] >= status["parallel_seconds"] >= 0
```

Starting the same root with `workers=1` must raise
`configuration_conflict`. Add a unit test that monkeypatches monotonic time and
proves `startup_timeout` replaces the fixed ten-second deadline.

- [ ] **Step 2: Verify cache tests fail**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_cache.py
```

Expected: failures identify missing metadata fields and arguments.

- [ ] **Step 3: Extend daemon metadata safely**

Bump `DAEMON_SCHEMA` to 2 and add `workers: int` to `CacheMetadata`.
Update strict metadata validation, tuple construction, command generation, and
configuration conflict comparison. Old schema-1 files must be treated as stale
metadata and replaced only when their recorded process is not accepted as a
live compatible daemon.

Construct the service context with:

```python
self.context = AgentContext.open(
    metadata.root,
    metadata.project_file or None,
    workers=metadata.workers,
    worker_memory_budget_bytes=metadata.max_memory_bytes,
)
```

- [ ] **Step 4: Record prewarm statistics**

Measure `prewarm_seconds` around the existing prewarm request. Copy the current
`AgentContext.parallel_stats` into cache status and add its fallback count to a
new `CacheStats.parallel_fallbacks` field. Keep budget enforcement after pool
shutdown.

- [ ] **Step 5: Replace the fixed startup deadline**

Extend `start_cache()` and `_start_cache_unlocked()` with
`startup_timeout: float = 120.0`. Reject non-positive values and calculate:

```python
deadline = time.monotonic() + startup_timeout
```

Do not write the client timeout into daemon metadata because it does not affect
daemon compatibility.

- [ ] **Step 6: Add cache CLI flags**

Add `--workers auto|N` and `--startup-timeout` to `cache start`, pass workers to
the hidden `cache serve` command, and pass both values through `_cache_start()`.
`view` and `index` also receive `--workers` and forward it to
`build_codebase_index()`.

- [ ] **Step 7: Run cache and CLI tests**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_cache.py tests/test_agent_worker.py tests/test_agent_codebase.py
PYENV_VERSION=venv3.14.0 pyenv exec ruff check delphi_lsp/agent_cache.py delphi_lsp/agent_cli.py tests/test_agent_cache.py
```

Expected: all selected checks pass.

- [ ] **Step 8: Commit cache lifecycle integration**

Commit with title `Expose bounded cache prewarm workers`.

### Task 5: Documentation, benchmark, and complete verification

**Files:**
- Modify: `README.md`
- Modify: `MANIFEST.in`
- Create: `scripts/benchmark_parallel_cache.py`
- Modify: `tests/test_agent_cache.py`
- Modify: `tests/test_package_metadata.py`

- [ ] **Step 1: Write failing documentation and manifest assertions**

Require README text for `--workers auto|N`, the four-worker cap, 128-MiB
automatic budget term, spawn processes, transient versus retained memory,
serial fallback, new status fields, and `--startup-timeout`.

Require:

```python
assert "include scripts/benchmark_parallel_cache.py" in manifest
```

- [ ] **Step 2: Verify documentation tests fail**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_cache.py tests/test_package_metadata.py
```

Expected: assertions for the new documentation and benchmark manifest entry
fail.

- [ ] **Step 3: Document the final CLI and memory contract**

Update the Agent CLI command synopsis and cache section without changing the
documented OpenCode history. Explain that automatic parallel workers are
short-lived and excluded from retained-cache accounting.

- [ ] **Step 4: Add a reproducible benchmark**

Create `scripts/benchmark_parallel_cache.py` with arguments
`--root`, `--workers`, and `--json`. It must run
`build_codebase_index(root, workers=1)` and a second build with the requested
workers, compare ordered symbol cards, and output:

```json
{
  "sequential_seconds": 1.0,
  "parallel_seconds": 0.5,
  "speedup": 2.0,
  "workers_effective": 4,
  "targets_equal": true
}
```

Return nonzero if targets differ. Add the script to `MANIFEST.in`.

- [ ] **Step 5: Run focused documentation and benchmark tests**

Run:

```bash
../../.venv/bin/pytest -q tests/test_agent_cache.py tests/test_package_metadata.py
../../.venv/bin/python scripts/benchmark_parallel_cache.py --root tests/fixtures --workers 2 --json
```

Expected: tests pass, benchmark reports equal targets, and the command exits
zero.

- [ ] **Step 6: Run complete quality gates**

Run:

```bash
PYENV_VERSION=venv3.14.0 pyenv exec ruff check delphi_lsp tests scripts/benchmark_parallel_cache.py
../../.venv/bin/pytest -q
../../.venv/bin/python -m build --outdir /tmp/python-delphi-lsp-parallel-build
../../.venv/bin/python -m twine check /tmp/python-delphi-lsp-parallel-build/*
git diff --check
```

Expected: Ruff, the complete test suite, wheel/sdist build, Twine, and diff
checks pass.

- [ ] **Step 7: Run a representative speed comparison**

Generate a temporary project with at least 32 nontrivial independent units and
run the benchmark with one and automatic workers. Record the sequential time,
parallel time, selected workers, and speedup in the final handoff. Do not turn
the measured ratio into a timing-sensitive CI assertion.

- [ ] **Step 8: Commit documentation and benchmark**

Commit with title `Document and benchmark parallel cache prewarming`.

### Task 6: Final review and cleanup

**Files:**
- Review all files changed since `28593e7`

- [ ] **Step 1: Inspect the complete diff**

Run:

```bash
git diff --stat 28593e7..HEAD
git diff 28593e7..HEAD --check
git status --short
```

Verify that no build output, cache metadata, benchmark corpus, token, or daemon
process is retained.

- [ ] **Step 2: Verify no cache daemons remain**

Run:

```bash
ps -axo pid=,comm=,args= | awk '$2 ~ /python/ && $0 ~ /-m delphi_lsp\\.agent_cache serve/'
```

Expected: no daemon rows.

- [ ] **Step 3: Record local completion**

Report the branch, HEAD commit, exact test count, benchmark ratio, worker count,
and build hashes. Do not push to `origin` or publish a new PyPI release unless
the user separately requests that external action.
