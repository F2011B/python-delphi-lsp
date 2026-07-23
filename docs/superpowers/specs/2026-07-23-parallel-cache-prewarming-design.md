# Parallel Cache Prewarming Design

## Goal

Reduce cold-start time for large Delphi codebases by parsing independent source
units concurrently before their results are merged into the existing bounded
in-memory navigation cache.

The implementation must preserve Protocol v2 output, stable target IDs,
deterministic result ordering, source invalidation, the retained-cache budget,
and the existing OpenCode worker boundary.

## Scope

Parallel outline and semantic-model construction applies to:

- `delphi-lsp-agent cache start`
- the first navigation build in `delphi-lsp-agent worker`
- `delphi-lsp-agent view`
- `delphi-lsp-agent index`

Project discovery and dependency resolution remain serial because they mutate a
shared project graph. Once that graph has produced an ordered unit list, each
unit can be read and parsed independently.

Relations, metrics, response pagination, cache eviction, and query execution
remain in the daemon process. They are not distributed across workers.

## Architecture

Add a focused parallel-outline module that owns worker selection, process-pool
lifecycle, task execution, result collection, and timing. Both the layered
codebase index and the Protocol v2 registry builder call this module instead of
parsing units in their own serial loops.

CPU-intensive parsing uses `ProcessPoolExecutor` with an explicit
`multiprocessing` `spawn` context on every platform. Threads are not used
because normal CPython parsing is constrained by the GIL. `spawn` also prevents
cache-daemon listener sockets, authentication tokens, locks, and unrelated
parent state from being inherited by worker processes.

Each worker receives only immutable task data:

- canonical source path
- compiler defines
- whether the caller needs source text returned

It reads the source, builds the outline semantic model, counts lines and
symbols, and returns a picklable result. Workers never mutate the workspace,
cache, progress state, or shared symbol index.

The parent process:

1. submits tasks in the canonical unit order;
2. collects completed tasks and emits monotonic progress;
3. sorts successful results back into canonical order;
4. registers models into one shared `SymbolIndex`;
5. builds the existing navigation registry and target IDs;
6. enforces the retained-cache budget after the worker pool has shut down.

Only the final parent-owned navigation structures are retained. Parser worker
processes are short-lived and are closed before the daemon becomes ready.

## Worker Selection

The CLI accepts `--workers auto|N`. `auto` is the default. Explicit values must
be integers from 1 through 32.

For cache and worker processes, automatic parallelism is:

```text
min(
    number_of_tasks,
    4,
    max(1, detected_cpu_count - 1),
    max(1, retained_cache_budget_bytes // 128_MiB),
)
```

The cache budget term is a conservative transient-memory guard. It does not
change the documented meaning of the retained-cache budget and does not claim
to impose a hard RSS limit.

The standalone `view` and `index` commands have no cache budget. Their
automatic worker count omits the budget term while retaining the task, CPU, and
four-worker caps.

An explicit `--workers N` overrides automatic CPU and memory selection but is
still reduced to the number of tasks. This makes higher concurrency an
intentional operator choice.

When zero or one task is effective, the implementation runs in-process without
creating a process pool.

## CLI and Status Contract

The following commands accept `--workers auto|N`:

```text
delphi-lsp-agent cache start
delphi-lsp-agent worker
delphi-lsp-agent view
delphi-lsp-agent index
```

`cache start` additionally accepts `--startup-timeout SECONDS`, defaulting to
120 seconds. This replaces the fixed ten-second client wait that is too short
for legitimate large workspaces. The timeout affects only the starting client,
not daemon idle shutdown.

Cache metadata records the configured worker value. Starting the same root
with a conflicting worker configuration returns the existing configuration
conflict error.

JSON cache status adds:

- `workers_configured`: `"auto"` or the explicit integer
- `workers_effective`: worker count used by the most recent registry build
- `parallel_files_completed`: number of source tasks completed
- `prewarm_seconds`: total prewarm duration
- `parallel_seconds`: time spent in the parallel outline stage
- `parallel_fallbacks`: number of automatic serial fallbacks

Existing status fields and stdout/stderr separation remain unchanged.

## Error and Fallback Behavior

Worker failures include the affected display path without exposing daemon
authentication data or arbitrary internal tracebacks.

Source read failures keep the current caller-specific behavior:

- the layered `view`/`index` builder skips unreadable sources;
- the Protocol v2 registry reports `source_unavailable`.

Parser failures remain fatal, matching current serial behavior.

If automatic mode cannot create or maintain a process pool before any result is
committed, it retries the batch once in-process and increments
`parallel_fallbacks`. Explicit `--workers N` does not silently fall back; it
reports a parallel-startup error so the operator's requested configuration is
not misrepresented.

Once any parallel result has been accepted, a broken pool fails the build
instead of mixing partial parallel output with an implicit retry.

Callback exceptions cancel outstanding tasks and propagate unchanged, matching
the current progress callback contract.

## Determinism and Invalidation

Completion order must never affect:

- model registration order
- symbol ordering
- overload ordinals
- parent target relationships
- target IDs
- Protocol v2 response order

Parallel and serial builds of the same unchanged workspace must produce equal
navigation cards and target IDs.

The existing workspace revision is checked before a build. A later request
continues to invalidate the cache when source metadata changes. Parallelism
does not add a persistent disk cache and does not weaken revision checks.

## Memory Behavior

The pool is created only for a cold build and shut down before cache-budget
measurement. At most the effective worker count of source/model results can be
in flight.

Returned source text is retained only when the Protocol v2 registry needs it
for later `inspect` calls. Layered `view` and `index` builds return models
without source text to reduce inter-process transfer.

After deterministic merge, temporary futures, task inputs, and worker results
are released before the existing auxiliary/navigation eviction sequence runs.
The 80-percent retained-cache warning contract is unchanged.

## Testing

Unit tests cover:

- parsing `auto` and explicit worker values;
- automatic worker selection for CPU, task count, and cache budget;
- no process pool when effective workers equal one;
- deterministic parallel versus serial model and target ordering;
- unreadable-source behavior for both callers;
- automatic serial fallback and explicit-mode failure;
- metadata configuration conflicts involving workers;
- status timing, counts, and fallback fields;
- startup timeouts longer than the old ten-second limit;
- callback exception propagation and pool cleanup;
- retained-cache warning and eviction behavior after a parallel build.

Cross-platform tests use top-level picklable worker functions and exercise
spawn semantics on Windows, macOS, and Linux.

A benchmark generates a representative multi-unit Delphi project and reports:

- sequential cold-build duration with `--workers 1`;
- automatic parallel cold-build duration;
- selected worker count;
- speedup ratio;
- equality of indexed targets.

The benchmark is evidence, not a timing-sensitive CI assertion. Release
acceptance requires identical results and an observed cold-build improvement on
a machine with at least four logical CPUs.

## Documentation

The README documents:

- automatic process parallelism;
- the four-worker and memory-aware caps;
- `--workers` and `--startup-timeout`;
- transient worker memory versus retained-cache accounting;
- status fields;
- serial fallback behavior.

## Non-Goals

- multiple cache daemons or cache sharding;
- parallel mutation of `ProjectIndexer`;
- persistent AST serialization to disk;
- parallel execution of individual Protocol v2 queries;
- changing OpenCode session/root worker ownership;
- a hard operating-system RSS limit.
