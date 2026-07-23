# Bounded CLI Cache Daemon Design

## Goal

Add a command that prewarms a Delphi workspace into the existing semantic
navigation structures and keeps them in a local background process so later,
separate `delphi-lsp-agent` command invocations reuse the in-memory state.
Bound retained cache memory, degrade safely when the budget is reached, and
make daemon lifecycle and cache behavior observable.

## Existing Behavior

`delphi-lsp-agent worker` already keeps an `AgentContext` alive while it serves
NDJSON on standard input and output. The generated OpenCode
`delphi_codebase` plugin starts one such worker per OpenCode session and
workspace root, so repeated tool calls in that session reuse project, symbol,
relation, metric, and focus state. Session deletion, transport failure, and
plugin disposal stop those workers.

Standalone `view` and `index` commands create a new process for every
invocation. They cannot reuse the OpenCode worker or each other's memory.

## Considered Approaches

### 1. Reuse a foreground NDJSON worker manually

Callers could keep `delphi-lsp-agent worker` open and write multiple requests
to its standard input. This already works, but independent shell invocations
cannot discover or communicate with that process. It does not satisfy the
requested CLI workflow.

### 2. Persist a serialized index on disk

Each command could load a pickle, SQLite database, or custom index. This avoids
a daemon and limits resident memory between calls, but it is not an in-memory
cache. Serialization compatibility, stale-file handling, and safe loading add
complexity, while large indexes still need to be reconstructed for each
process.

### 3. Run one bounded local daemon per workspace root

This is the selected approach. A background Python process owns one
`AgentContext`, prewarms the symbol navigation registry, accepts authenticated
loopback requests from later CLI processes, and evicts recomputable heavy
structures when its cache budget is exceeded. It reuses the package's proven
Protocol v2 request and response model and preserves the existing standalone
commands.

## Command-Line Interface

The lifecycle commands are:

```text
delphi-lsp-agent cache start --root PATH
    [--project-file FILE]
    [--max-memory 512M]
    [--idle-timeout 1800]

delphi-lsp-agent cache status --root PATH [--format text|json]
delphi-lsp-agent cache stop --root PATH
```

`cache start` is idempotent for a live daemon at the same canonical root. It
starts a detached process, waits until prewarming and metadata publication
finish, then prints the daemon status. A live daemon with different requested
configuration is reported without silently replacing it. Stale metadata from
a dead process is removed before startup.

Protocol queries use the daemon:

```text
delphi-lsp-agent query --root PATH open
delphi-lsp-agent query --root PATH find TCustomer
delphi-lsp-agent query --root PATH focus TARGET_ID
delphi-lsp-agent query --root PATH inspect TARGET_ID --detail body
delphi-lsp-agent query --root PATH trace TARGET_ID --relation callers
delphi-lsp-agent query --root PATH problems
delphi-lsp-agent query --root PATH metrics [UNIT_QUERY]
```

The query command also accepts the existing Protocol v2 pagination and context
limits where relevant. It writes one Protocol v2 JSON response to standard
output. It fails clearly when no live daemon exists; it does not silently
create a background process.

`view`, `index`, `worker`, installer commands, and the generated OpenCode
plugin retain their existing behavior.

## Daemon Discovery and Transport

The daemon binds only to `127.0.0.1` on an operating-system-assigned port. It
atomically publishes a metadata file beneath:

```text
ROOT/.delphi-lsp/agent-cache/daemon.json
```

The metadata contains the schema version, canonical root, process ID, port,
random authentication token, package version, project selection, configured
budget, idle timeout, and startup time. On POSIX systems the directory and file
are owner-only. Clients validate the schema and canonical root before
connecting. Every socket request must present the random token; invalid tokens
receive a generic authentication error.

Each CLI invocation opens a short-lived loopback connection and exchanges one
newline-delimited JSON request and response. A lock serializes access to the
stateful `AgentContext`, ensuring focus and lazy-cache mutations remain
consistent when several shells query concurrently.

## Cache Contents and Prewarming

Startup opens the workspace and selected project through `AgentContext`, then
forces construction of the semantic symbol registry with a minimal `find`
request. This caches the internal navigation structure rather than retaining a
second full parser AST. Project outlines, unit and symbol identities, source
documents, and semantic declarations are warm after startup. Relation indexes
and architecture metrics remain lazy because many sessions never request them.

The existing workspace revision calculation runs before every request. A
changed reachable source, include, project configuration, search path, or
define invalidates the affected in-process caches before the query executes.
Stable target focus is preserved only when it still resolves in the rebuilt
registry.

## Memory Budget and Degradation

The default retained-cache budget is 512 MiB and is configurable with
`--max-memory`. The daemon owns exactly one root and does not accumulate caches
for unrelated workspaces.

The budget applies to retained, recomputable navigation caches, not to total
Python process RSS or the temporary peak needed while parsing a source file.
A portable hard RSS limit would turn a recoverable large project into an
allocation crash. Instead, the implementation estimates the deep size of the
owned workspace, symbol, source-document, relation, and metric structures
without traversing modules or global runtime objects.

After prewarming and after every request, the daemon compares that estimate to
the budget. If it exceeds the limit, it:

1. drops relation and metric caches;
2. drops the full symbol/source-document registry if still over budget;
3. retains the smaller project and unit catalog needed for discovery;
4. marks the cache state `compact`.

The completed response is returned before eviction. A later semantic query can
rebuild the required structure and is therefore slower, but remains
functional. It is evicted again after the response if it still exceeds the
budget. No correctness result is served from a partially evicted structure.

The daemon exits after 30 minutes without a query by default. Explicit
`cache stop`, process termination, or idle shutdown removes its metadata when
the current process still owns that metadata record.

## Status and Diagnostics

`cache status` reports:

- daemon PID, root, project, uptime, and last activity;
- configured budget and current estimated retained bytes;
- cache state (`warming`, `warm`, or `compact`);
- requests, warm hits, rebuilds, invalidations, and evictions;
- idle timeout and remaining idle time;
- current workspace revision when available.

Human-readable output is concise. JSON output is deterministic and contains no
source text or authentication token. Protocol errors and daemon diagnostics go
to standard error; successful query JSON remains clean on standard output.

## Failure Handling

- Missing or stale metadata produces a specific `cache_not_running` error.
- Startup failure removes incomplete metadata and returns the child diagnostic
  without leaving an orphan daemon.
- Authentication and malformed transport requests never reach
  `AgentContext`.
- A request exception uses the same sanitized Protocol v2 error policy as the
  current worker.
- Broken clients do not terminate the daemon.
- `cache stop` is idempotent when stale metadata is present.
- Metadata cleanup verifies PID identity and token ownership before unlinking
  the current record.

## Testing

Implementation follows test-driven development:

1. parser tests cover lifecycle commands, ergonomic query translation, memory
   suffixes, and defaults;
2. unit tests cover metadata validation, cache-size estimation, eviction order,
   status sanitization, authentication, and idle decisions;
3. integration tests start a real daemon, make queries from separate Python
   processes, verify one PID is reused, inspect status, and stop it cleanly;
4. a deliberately tiny budget forces `compact` mode and proves later queries
   still return correct results;
5. editing a reachable Delphi source proves revision invalidation and rebuilt
   query results;
6. existing worker and generated OpenCode runtime tests prove their current
   per-session caching and cleanup remain unchanged;
7. the complete test suite, formatting checks, package build, and metadata
   validation run before completion.

## Non-Goals

- Sharing one daemon between unrelated roots.
- Persisting or restoring the semantic registry across machine restarts.
- Replacing the LSP server's document lifecycle.
- Making `view` and `index` implicitly start a daemon.
- Enforcing a hard operating-system RSS limit.
