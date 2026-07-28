# Python Delphi LSP

`python-delphi-lsp` parses Delphi/Object Pascal, builds semantic and project
indexes, serves LSP, and provides bounded codebase navigation for agents.
Version 3.1.0 is authored by Dark Light and supports Windows, macOS, and Linux.

## Install and quick start

Install into the Python environment that will run the command:

```bash
python -m pip install python-delphi-lsp
```

On Windows, use `py -m pip install python-delphi-lsp` if that is your system
convention. On macOS and Linux, use `python3 -m pip install python-delphi-lsp`
when `python` is unavailable. Normal installed use needs neither a checkout
nor `PYTHONPATH`.

```python
from delphi_lsp import parse

result = parse("unit Unit1; interface implementation end.", "Unit1.pas")
print(result.root)
```

For semantic work across units, `build_workspace_semantics` returns per-file
models and a shared symbol index. `ProjectIndexer` follows a project entry
with explicit search paths, include paths, and defines.

```python
from delphi_lsp import ProjectIndexer, build_workspace_semantics

workspace = build_workspace_semantics({
    "Unit1.pas": "unit Unit1; interface uses Unit2; implementation end.",
    "Unit2.pas": "unit Unit2; interface implementation end.",
})
print(workspace.index.lookup("Unit2"))

project = ProjectIndexer(
    search_paths=["src"], include_paths=["include"], defines=["DEBUG"]
).index("Main.dpr")
print(project.parsed_units)
```

Long-running discovery and indexing accept a keyword-only `on_progress`
callback. It receives an immutable `ProgressEvent` with package-controlled
phase, path, and monotonic counters; callback exceptions are not suppressed.

```python
from delphi_lsp import ProjectIndexer, ProgressEvent

def report(event: ProgressEvent) -> None:
    print(event.phase, event.files_completed, event.path)

ProjectIndexer(on_progress=report).index("Main.dpr")
```

## Architecture metrics

The public metrics API analyzes a single unit or aggregates a complete project:

```python
from delphi_lsp import analyze_project, analyze_unit

unit = analyze_unit(
    "unit Alpha; interface implementation procedure Run; begin end; end.",
    "Alpha.pas",
)
print(unit.lines.total_lines, unit.cyclomatic.maximum)

project = analyze_project({
    "Main.dpr": "program Main; uses Alpha; begin end.",
    "Alpha.pas": "unit Alpha; interface implementation end.",
})
print(project.total_loc)
```

Line results distinguish total, source, blank, comment-only, mixed-comment, and
compiler-directive lines. Project `total_loc` counts each `.dpr`, `.dpk`, and
`.pas` source once; `include_loc` counts unique `.inc` inputs separately, and
`total_loc_with_includes` combines both totals.

Cyclomatic complexity is reported per routine and as unit/project aggregates.
The result also includes complete Halstead counts and derived values, a
normalized 0–100 maintainability index, symbol counts, dependency edges,
afferent coupling (fan-in), efferent coupling (fan-out), instability,
abstractness, and distance from the main sequence. Coupling detail separates
internal from external dependencies. Empty or partial inputs produce finite
JSON values; unreadable agent-workspace inputs are reported as metric problems.

Run the stdio language server with `delphi-lsp`, or equivalently:

```bash
python -m delphi_lsp.lsp_server
```

## OpenCode LSP configuration

The root `opencode.json` starts the installed package portably:

```json
{
  "lsp": {
    "delphi": {
      "command": ["delphi-lsp"],
      "extensions": [".pas", ".dpr", ".dpk", ".inc"],
      "initialization": {"autoDiscoverPaths": true}
    }
  }
}
```

`autoDiscoverPaths` is the default. It discovers compiler context without an
environment section; LSP remains available for normal editor and OpenCode use,
including large sources.

The LSP builds its structural index through the same optimized outline path for
every source, with no file-size threshold. Definition, hover, references,
rename, completion, document symbols, workspace symbols, and diagnostics remain
registered for every file size; source-aware fallbacks keep body-level queries
available without returning the complete file as agent context.

### Automatic discovery

Auto-discovery reads `.dpr`, `.dpk`, `.dproj`, `.cfg`, and `.dof` files.
Its resolution order is:

1. An explicit `.dproj`, `.dpr`, or `.dpk` selection takes precedence. An
   explicit `.dproj` resolves its `MainSource`.
2. Otherwise, `.dpr` and `.dpk` candidates are considered.
3. `MainSource` in a `.dproj` contributes its entry project.
4. A selected entry associates same-stem `.dproj`, `.cfg`, and `.dof`.
5. Unit search paths, include paths, and defines are accumulated from explicit
   settings, project metadata, and the associated settings files.
6. Direct `Unit in 'path/Unit.pas'` references contribute their parent
   directory to unit search paths.

A single discovered project is selected automatically. With multiple project
entries, a synthetic repository workspace containing every supported source is
selected automatically while the concrete projects remain available. With no
project entry, the same workspace model is used. Scans skip build and cache
directories such as `build`, `dist`, environments, VCS folders, `node_modules`,
and tool caches. Missing paths and invalid metadata become problems; paths are
not guessed.

## Agent CLI and Interface/Protocol v3

`delphi-lsp-agent` has these subcommands and options:

```text
delphi-lsp-agent cache start --root PATH [--project-file FILE] [--max-memory 512M]
                                  [--max-disk-cache 512M]
                                  [--workers auto|N] [--startup-timeout 120]
                                  [--idle-timeout 1800]
delphi-lsp-agent cache status --root PATH [--format text|json]
delphi-lsp-agent cache stop --root PATH
delphi-lsp-agent cache clear --root PATH
delphi-lsp-agent view --root PATH [--project-file FILE] --layer LAYER
                      [--query TEXT] [--format markdown|json] [--deep-projects]
                      [--workers auto|N]
delphi-lsp-agent index --root PATH [--project-file FILE] [--out FILE]
                       [--workers auto|N]
delphi-lsp-agent wiki export --root PATH [--project-file FILE] [--out DIRECTORY]
                             [--workers auto|N] [--force] [--quiet]
delphi-lsp-agent query --root PATH ACTION [VALUE]
                      [--project-id ID] [--detail summary|declaration|members|context|body|implementations]
                      [--relation references|callers|callees|uses|used_by|inherits|implements]
                      [--graph ast|cfg|dfg|call|full] [--direction out|in|both]
                      [--depth 1..16]
                      [--cursor TEXT] [--max-items INT] [--max-chars INT]
delphi-lsp-agent skill install [--target PATH] [--force]
delphi-lsp-agent opencode install [--target PATH] [--python PYTHON]
                                  [--force] [--write-agent|--write-config]
delphi-lsp-agent worker --root PATH [--project-file FILE] [--workers auto|N]
```

The `cache` commands manage one daemon per canonical root. Use these:

```bash
delphi-lsp-agent cache start --root PATH
delphi-lsp-agent cache status --root PATH
delphi-lsp-agent cache stop --root PATH
delphi-lsp-agent cache clear --root PATH
```

`cache start` outputs cache lifecycle JSON; runtime warnings are still on stderr.
`cache status --format json` outputs status JSON to stdout and the same warning stream on stderr.
`cache stop` outputs stop status JSON and may include warnings on stderr.
`cache clear` stops the daemon and removes persistent navigation shards.
`query` outputs Protocol v3 JSON responses and writes warnings to stderr.
For `index`, a relative `--out` is resolved below `--root`; the default is
`.delphi-lsp/agent-index/index.json`.

### Open Knowledge Format wiki export

Export the complete unique knowledge represented by the layered codebase views
as a portable Markdown wiki:

```bash
delphi-lsp-agent wiki export --root PATH --out codebase-wiki
```

When `--out` is relative, it is resolved below `--root`; the default is
`.delphi-lsp/wiki`. The command builds the semantic index once, deep-indexes
main projects, calculates workspace and unit metrics, and writes:

- an OKF 0.2 root `index.md`;
- linked project, unit, and symbol concepts;
- declaration and implementation source fragments;
- resolved and unresolved semantic references;
- discovery/project problems and complete metric records;
- Protocol v3, relation, CPG, cache, and layer-mapping reference concepts.

Without `--project-file`, the exporter selects repository main projects:
non-example project entries in the repository root, or, when none exist, the
shallowest configured `.dproj` entries. Recursively discovered example,
sample, test, benchmark, and vendor projects are not promoted to project
pages. A repository `.delphi-lsp.toml` can replace this heuristic with the
explicit monorepo selection described below. With `--project-file`, only that
explicit entry is selected. Project
dependency traversal and include loading are confined to the repository root,
so Delphi SDK and other external standard units are recorded as unresolved
dependencies instead of being parsed. If no main project can be identified,
the exporter falls back to the repository source inventory without inventing
project pages.

Every non-index concept is UTF-8 Markdown with YAML frontmatter and a non-empty
`type`. Readable filenames include a stable digest, so overloads and equal names
from different units remain distinct even on case-insensitive filesystems.
Output order and content are deterministic. The finished bundle replaces the
destination only after generation succeeds; an existing non-empty destination
requires `--force`, and unsafe root/ancestor/symlink targets are rejected.

Pages are streamed to disk, decoded source retention is bounded, and metrics
process source paths without materializing the entire source corpus. Lazy CPG
subgraphs are documented rather than eagerly multiplied across all possible
targets; a focused live `cpg` query remains the bounded way to obtain one graph.
The bundle structure follows the
[Open Knowledge Format 0.2 specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md).

Progress is shown on `stderr` while the final machine-readable JSON summary
remains the only record on `stdout`. Redirected progress is throttled to
milestones; pass `--quiet` to suppress it completely.

```bash
delphi-lsp-agent query --root PATH find TCustomer
delphi-lsp-agent query --root PATH focus TARGET_ID
delphi-lsp-agent query --root PATH inspect
delphi-lsp-agent query --root PATH trace TARGET_ID --relation callers
delphi-lsp-agent query --root PATH cpg TARGET_ID --graph full --direction out --depth 4
delphi-lsp-agent query --root PATH metrics
delphi-lsp-agent query --root PATH metrics UNIT_QUERY
delphi-lsp-agent query --root PATH metrics --target-id UNIT_TARGET_ID
delphi-lsp-agent cache status --root PATH --format json
```

A `target_v2_...` positional value for `metrics` is also recognized as a unit
target ID; other positional values remain case-insensitive unit-name or path
queries.

The repository root is sufficient even when it contains many projects:

```bash
delphi-lsp-agent cache start --root PATH
delphi-lsp-agent query --root PATH find TCustomer
```

This prewarms one bounded repository navigation index and requires no project
selection. A `.dproj` is optional. For repeatable monorepo selection, add a
`.delphi-lsp.toml` file as described below. Pass one project explicitly when
its project-specific compiler
settings and `MainSource` should define the index:

```bash
delphi-lsp-agent cache start --root PATH --project-file relative/path/Main.dproj
```

An explicit `.dpr` or `.dpk` remains supported. To switch an already running
repository cache to one of its concrete project views, list the projects and
focus one by its returned `project_id`:

```bash
delphi-lsp-agent query --root PATH open
delphi-lsp-agent query --root PATH focus --project-id PROJECT_ID
```

Selecting a project this way also prewarms its navigation cache.

### TOML project selection for monorepos

Place `.delphi-lsp.toml` in the repository root to define which `.dpr`,
`.dpk`, or companion `.dproj` projects belong to the codebase:

```toml
[projects]
include = [
  "apps/**",
  "services/Api/ApiServer.dproj",
  "packages/*/Runtime.dpk",
]
exclude = [
  "**/examples/**",
  "**/tests/**",
]

[workspace]
exclude = [
  "vendor",
  "generated/**",
  "thirdparty/legacy",
]
```

Paths are relative to the repository root, case-insensitive, and use `/` on
every operating system. An exact directory name covers all descendants. `*`
matches within one path segment, while `**` crosses directory boundaries.
Absolute paths and `..` traversal are rejected.

When `include` is non-empty, only matching project entries remain. Matching a
`.dproj` selects the `.dpr` or `.dpk` from its `MainSource`. The `exclude`
list is applied afterward, so `exclude` always wins. With active TOML filters,
all remaining matches are main projects even when they occur at different
directory depths; the built-in shallow-project and example-name heuristic is
not applied.

Use `projects.exclude` when a directory may still contain shared units but its
project entries must not become selectable projects. Use `workspace.exclude`
for a hard boundary. Matching directories are never traversed or parsed, and
neither is any file below them. They cannot be reintroduced by
`projects.include` or an explicit `--project-file`. Unit dependencies and
include directives resolving into those directories are reported as
unavailable without reading the files.
For complete directory pruning, an exact entry such as `"vendor"` is enough;
`"generated/**"` also excludes the `generated` directory itself.

The same selection is used by `cache`, `worker`, `query`, `view`, `index`,
wiki export, and language-server discovery. An explicit `--project-file`
bypasses `projects.include` and `projects.exclude` for that invocation, but
never bypasses `workspace.exclude`. If no project remains, the command reports
that the configured filters matched no Delphi projects and stops instead of
silently parsing every source file in the repository. Restart an already
running cache daemon after changing the path configuration.

`inspect` uses the currently focused target, so call `focus TARGET_ID` before
`inspect` unless a previous request already selected it.

The cache daemon prewarms the navigation cache at startup so first `find` requests are
fast. The cache retained-cache budget is `512 MiB` by default and tracks retained
cache usage only, not a hard RSS/parse peak. Warnings are emitted on stderr at or
above 80 percent.

Cold builds parse independent source units in short-lived processes created with
the cross-platform `spawn` method. `--workers auto|N` defaults to `auto`.
Automatic selection uses at most eight worker processes, leaves one detected CPU
free, never exceeds the source task count, and—for the cache daemon—allows one
worker per `64 MiB` of retained-cache budget. `view` and `index` use the same
task, CPU, and eight-worker caps without the cache-budget term. An explicit value
from 1 through 32 overrides the automatic CPU and memory caps but is still
limited by the number of tasks.

Worker processes build compact, detached navigation shards and exit before
retained-cache accounting. Full semantic object graphs and source text are not
retained in the parent. Tokenized source documents are loaded only for source
evidence requests such as `inspect`, and a byte-bounded LRU evicts older
documents. The transient worker memory remains separate from retained navigation
structures and the existing 80-percent warning. Retained bytes are maintained
incrementally; requests never traverse the complete cache object graph merely
to measure it. If an automatic pool fails before accepting a result, one
automatic serial fallback is attempted; explicit worker counts fail instead of
silently changing the requested configuration.

Detached navigation shards are also stored as versioned, content-addressed JSON
under `.delphi-lsp/agent-cache/navigation-v1`. A restarted CLI or OpenCode cache
daemon reuses unchanged units without parsing them again. Source content,
conditional defines, or a shard-schema change produces a cache miss; malformed
or incompatible JSON is ignored and rebuilt. The disk cache contains no pickle
payloads and does not count against the retained-RAM budget. Each completed
navigation build removes superseded shards and enforces the
`--max-disk-cache` byte budget, which defaults to 512 MiB.

Cache prewarming builds the navigation registry directly without constructing
an empty-query result, symbol cards, pagination, or JSON payloads. Up to sixteen
recent ranked queries are retained in a small LRU so alternating CLI and
OpenCode searches remain warm.

`cache start` waits up to `--startup-timeout 120` seconds for the child process
to bind and publish readiness metadata, not for the large workspace to finish
prewarming. Workspace discovery and prewarming continue in the daemon while
status reports `warming`. If bootstrap fails, the CLI reports whether it timed
out or exited, the exit code, Python executable, workspace, and a sanitized
tail of child stderr. Starting a live root with a different worker
configuration reports a configuration conflict.

Eviction is ordered: auxiliary caches are evicted first, navigation caches second.
If compaction removes navigable data, the daemon rebuilds the navigation state on demand
while preserving focus state for the next request.

The daemon tracks a 30-minute idle timeout; idle state shows in JSON status (`cache status`).
`source revision` changes on source edits and invalidate reused request caches.
Workspace state appears in status as `requests`, `warm_hits`, `rebuilds`, `invalidations`,
`evictions`, and `cache_state`. Parallel prewarm status adds
`workers_configured`, `workers_effective`, `parallel_files_completed`,
`prewarm_seconds`, `parallel_seconds`, `parallel_fallbacks`,
`navigation_disk_hits`, and `navigation_disk_misses`.

Metadata is stored in `.delphi-lsp/agent-cache/daemon.json` with owner-only token and
permissions (`daemon.json` mode 600 and parent 700). Do not copy or share this token
outside the root workspace.

`view --layer` accepts `overview`, `projects`, `units`, `unit`,
`symbols`, `symbol`, `implementation`, `references`, `problems`, and
`metrics`. For example, `delphi-lsp-agent view --layer metrics --format json`
returns a project summary and detailed unit metric objects; `--query` filters
units by name or path.
`index` materializes overview, projects, and problems JSON. `wiki export`
materializes the same navigational knowledge as a linked OKF Markdown bundle. `skill install`
writes the skill; `opencode install` writes the package-named skill, Markdown
agent, and plugin. The two deprecated write flags are harmless aliases and do
not change user configuration.
`worker` serves NDJSON over standard input/output.

Protocol v3 actions are `open`, `find`, `inspect`, `trace`, `focus`,
`problems`, `metrics`, and `cpg`. A `metrics` request without a query returns the
project summary followed by unit cards. A query filters units, while a unit
`target_id` from `open` selects one unit; `detail: "members"` adds routine,
Halstead, dependency, and symbol-count detail without returning source text.
Detail values are `summary`, `declaration`, `members`,
`context`, `body`, and `implementations`. Relations are `references`,
`callers`, `callees`, `uses`, `used_by`, `inherits`, and
`implements`.

A request requires `action` and can include `query`, `target_id`,
`project_id`, `detail`, `relation`, `graph`, `direction`, `depth`, `cursor`,
`max_items`, and `max_chars`. Defaults are empty text fields,
`detail: "summary"`, no relation, `graph: "full"`, `direction: "out"`,
`depth: 4`, `max_items: 12`, and `max_chars: 12000`. Depth is 1–16;
response ranges are 1–50 items and 256–40000 characters. A successful envelope
has `schema: 3`,
`workspace_revision`, `focus` (project, unit, and target IDs), `result`,
`page`, and `context`; errors have `schema: 3` and a code/message. Existing
target IDs remain unchanged across the protocol upgrade.

Focus preserves the selected project, unit, or target. Cursors bind a workspace
revision and request fingerprint, so source changes and cross-target or
cross-detail reuse invalidate them. `max_items` and `max_chars` bound each
response. A `sound_partial` relation is sound but incomplete: unresolved and
ambiguous relations are never fabricated. Unsupported relations are rejected.

### Lazy code property graph

The `cpg` action translates one selected unit, type, or routine into a bounded
code property graph. Graph selectors are `ast`, `cfg`, `dfg`, `call`, and `full`;
`direction` accepts `out`, `in`, or `both`, and `depth` limits traversal
from the selected target. Together, `graph`, `direction`, and `depth` select
the returned subgraph. Results contain stable CPG node and edge IDs,
location and symbol properties, graph metadata, and explicit problems.

AST edges preserve syntax containment. CFG edges include routine entry/exit,
sequence, conditional branches, loop back-edges, and conservative structured
control flow. Local DFG edges distinguish definitions, uses, and reaching
definitions. CALL edges are emitted only for a uniquely resolved explicit
callee. Ambiguous or unsupported semantics increase the unresolved count and
are reported as `sound_partial`; the navigator does not fabricate certainty.

CPG construction is lazy: navigation prewarming creates no graph. The first
request parses only the selected source, and identical requests reuse an
in-memory LRU keyed by workspace revision, target, `graph`, `direction`, and
`depth`. The CPG LRU receives 20 percent of the configured retained-cache
budget, capped at 128 MiB. Source text and full parser trees are not retained
inside graph records, and normal navigation keeps its existing cache path.

The release gate used the pinned 8,549-file FPC corpus with 4,021,192 physical
lines on an Apple-silicon Mac with Python 3.14 and eight workers. Navigation
prewarming took 22.62 seconds. The first focused CPG query took 0.0166 seconds;
the cached query took 0.000069 seconds and retained 4,007 bytes. Exactly one
source was parsed for CPG. Legacy navigation retained 98.43 percent of its
in-run control throughput, above the 95-percent release floor. Reproduce it
with `python scripts/benchmark_cpg.py --root CORPUS --query Run --workers 8`.

For every source size the navigator builds an outline first, loads source detail
lazily for a selected target, and returns only selected fragments. Typed source
chunks are at most 6000 characters and also respect the response budget. This
optimization does not remove LSP functionality.

## OpenCode semantic navigator

Install the generated integration in a worktree:

```bash
delphi-lsp-agent opencode install --target .
```

It writes:

```text
.agents/skills/python-delphi-lsp/SKILL.md
.opencode/plugins/delphi_codebase.ts
.opencode/agents/python-delphi-lsp.md
```

The package-named Markdown agent enables only the
`python-delphi-lsp` skill and `delphi_codebase`. It denies
`bash`, `read`, `glob`, `grep`, and `lsp`, along with edit/write and
other raw source tools. The skill is enabled. The installer does not use the
retired `.opencode/tools` path and never reads or changes `opencode.json`; that
file remains entirely user-owned. The deprecated `--write-config` and
`--write-agent` options are accepted harmlessly for compatibility.

The plugin maintains one worker per session/root, reusing focus, indexes, and
lazy CPG subgraphs.
During compaction it restores the focus and summary into the new context.
Transport failure, session deletion, and plugin disposal clean up the worker.

OpenCode history: 1.1.0 and 1.1.1 used a spawned view per call model.
Persistent session/root worker support first shipped in 2.0.0.
This is the same persistent session/root worker boundary.
The OpenCode worker stays separate from CLI daemon. Protocol v3 adds CPG
arguments without changing the persistent session/root boundary.

A generated OpenCode agent starts with this Markdown frontmatter:

```markdown
---
description: Inspect Delphi and Object Pascal codebases through python-delphi-lsp.
mode: all
temperature: 0
permission:
  delphi_codebase: allow
  skill:
    "*": deny
    python-delphi-lsp: allow
  lsp: deny
  bash: deny
  read: deny
  glob: deny
  grep: deny
  list: deny
  edit: deny
  write: deny
  patch: deny
  task: deny
  webfetch: deny
  websearch: deny
  question: deny
  todowrite: deny
  todoread: deny
  codebase_map: deny
  code_guidelines: deny
---
```

Select `python-delphi-lsp`, ask it to load the `python-delphi-lsp` skill, then
use `delphi_codebase` actions such as
`open`, `find`, `focus`, `inspect`, and `cpg`. Use semantic tool calls, not raw
source tools.

For architecture questions, call `metrics` without a query to compare unit
cards and read project LOC. Then select a returned unit ID with another
`metrics` call and `detail: "members"` to inspect its routines and coupling.

For the root LSP configuration, a local model can be used as follows:

```bash
opencode run --dir . --model ollama/ornith-lspctx --agent vllm-lsp \
  "Find the declaration of a Delphi symbol through LSP."
```

Use `--agent vllm-lsp-edit` only for the separate, focused LSP/edit
verification workflow; the semantic navigator agent above remains restricted
to its named skill and tool.

That separate verification uses `vllm/ornith-lspctx` and accepts the focused
LSP result `edit:Edit applied successfully`; it is not the semantic navigator
workflow and does not grant the navigator any raw source tools.

## Reproducible large-project vLLM proof

The proof generates a 117,511-line project. The verifier requires `skill`, `open` (`Main.dpr` evidence), `find`, `focus`, and `inspect`,
checks `MegaProc02500` and `Value := Value + 40`, and forbids raw `bash`,
`read`, `glob`, and `grep`. It then waits for the final answer and requires the
exact body range `src/Mega100kUnit.pas:117464-117509` and the inspected
statement in that answer. The prompt requires returned range metadata instead
of a model-calculated line inside the source fragment.
The proof uses the local Ornith vLLM OpenAI-compatible
endpoint at `http://127.0.0.1:8001/v1`.

Default scripts are offline and must not redownload the model. First check the
cache:

```bash
python scripts/check_ornith_cache.py --require-complete
```

With an already-running endpoint on any supported platform, run:

```bash
python scripts/bootstrap_vllm_codebase_skill_test.py --use-running-server
```

On macOS, the offline cached auto-start path is:

```bash
python scripts/bootstrap_vllm_codebase_skill_test.py --start-vllm
```

Automatic local vLLM startup is macOS-only. The package and OpenCode plugin are
supported on Windows. On Windows, start an OpenAI-compatible vLLM endpoint, then
run the Python bootstrap from PowerShell:

```powershell
python .\scripts\bootstrap_vllm_codebase_skill_test.py --use-running-server
```

For an endpoint at another URL, add `--base-url`:

```powershell
python .\scripts\bootstrap_vllm_codebase_skill_test.py --use-running-server --base-url http://127.0.0.1:9000/v1
```

`--skip-install` is an optional acceleration for an already prepared `.venv`;
omit it on a clean checkout so the bootstrap installs `.[dev]`.
The final-answer verifier defaults to a 420-second probe timeout. On a slower
local model server, increase it explicitly with `--probe-timeout SECONDS`.

The bundled automatic helper is not a cross-platform startup mechanism.

The architecture-metrics proof uses a separate deterministic 34-LOC project.
It requires the restricted model to load the skill, call `metrics` for the
project and most-complex unit, and report exact LOC, cyclomatic maximum, and
instability values. Raw source, search, shell, and write tools remain forbidden:

```bash
python scripts/bootstrap_vllm_codebase_skill_test.py --probe metrics --use-running-server
```

Use `--probe metrics --start-vllm --max-model-len 24576` for the cached macOS
auto-start path when no endpoint is already running.

## Migration to 2.0

`delphi_lsp` is the only supported import namespace. Update imports directly;
there is no compatibility import alias.

## Verification and limitations

For a checkout:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m build
python -m twine check dist/*
```

CI tests Ubuntu, macOS, and Windows on Python 3.10 and 3.14, then builds and
smoke-installs the wheel on Ubuntu/Python 3.14. Results depend on available
project files, defines, includes, and paths; unsupported compiler behavior and
unresolvable references are reported as problems. The vLLM proof additionally
requires OpenCode, a local endpoint, and a complete local cache.

## License

Mozilla Public License 2.0. See [LICENSE](LICENSE).
