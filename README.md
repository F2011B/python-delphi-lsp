# Python Delphi LSP

`python-delphi-lsp` parses Delphi/Object Pascal, builds semantic and project
indexes, serves LSP, and provides bounded codebase navigation for agents.
Version 2.0.4 is authored by Dark Light and supports Windows, macOS, and Linux.

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

1. An explicit project selection takes precedence.
2. Otherwise, `.dpr` and `.dpk` candidates are considered.
3. `MainSource` in a `.dproj` contributes its entry project.
4. A selected entry associates same-stem `.dproj`, `.cfg`, and `.dof`.
5. Unit search paths, include paths, and defines are accumulated from explicit
   settings, project metadata, and the associated settings files.
6. Direct `Unit in 'path/Unit.pas'` references contribute their parent
   directory to unit search paths.

A single discovered project is selected automatically. With no project entry,
the server uses a synthetic workspace of supported sources. Scans skip build
and cache directories such as `build`, `dist`, environments, VCS folders,
`node_modules`, and tool caches. Missing paths and invalid metadata become
problems; paths are not guessed.

## Agent CLI and Interface/Protocol v2

`delphi-lsp-agent` has these subcommands and options:

```text
delphi-lsp-agent cache start --root PATH [--project-file FILE] [--max-memory 512M]
                                  [--idle-timeout 1800]
delphi-lsp-agent cache status --root PATH [--format text|json]
delphi-lsp-agent cache stop --root PATH
delphi-lsp-agent view --root PATH [--project-file FILE] --layer LAYER
                      [--query TEXT] [--format markdown|json] [--deep-projects]
delphi-lsp-agent index --root PATH [--project-file FILE] [--out FILE]
delphi-lsp-agent query --root PATH ACTION [VALUE]
                      [--project-id FILE] [--detail summary|declaration|members|context|body|implementations]
                      [--relation references|callers|callees|uses|used_by|inherits|implements]
                      [--cursor TEXT] [--max-items INT] [--max-chars INT]
delphi-lsp-agent skill install [--target PATH] [--force]
delphi-lsp-agent opencode install [--target PATH] [--python PYTHON]
                                  [--force] [--write-agent|--write-config]
delphi-lsp-agent worker --root PATH [--project-file FILE]
```

The `cache` commands manage one daemon per canonical root. Use these:

```bash
delphi-lsp-agent cache start --root PATH
delphi-lsp-agent cache status --root PATH
delphi-lsp-agent cache stop --root PATH
```

`cache start` outputs cache lifecycle JSON; runtime warnings are still on stderr.
`cache status --format json` outputs status JSON to stdout and the same warning stream on stderr.
`cache stop` outputs stop status JSON and may include warnings on stderr.
`query` outputs Protocol v2 JSON responses and writes warnings to stderr.

```bash
delphi-lsp-agent query --root PATH find TCustomer
delphi-lsp-agent query --root PATH focus TARGET_ID
delphi-lsp-agent query --root PATH inspect
delphi-lsp-agent query --root PATH trace TARGET_ID --relation callers
delphi-lsp-agent query --root PATH metrics
delphi-lsp-agent query --root PATH metrics UNIT_QUERY
delphi-lsp-agent cache status --root PATH --format json
```

`inspect` uses the currently focused target, so call `focus TARGET_ID` before
`inspect` unless a previous request already selected it.

The cache daemon prewarms the navigation cache at startup so first `find` requests are
fast. The cache retained-cache budget is `512 MiB` by default and tracks retained
cache usage only, not a hard RSS/parse peak. Warnings are emitted on stderr at or
above 80 percent.

Eviction is ordered: auxiliary caches are evicted first, navigation caches second.
If compaction removes navigable data, the daemon rebuilds the navigation state on demand
while preserving focus state for the next request.

The daemon tracks a 30-minute idle timeout; idle state shows in JSON status (`cache status`).
`source revision` changes on source edits and invalidate reused request caches.
Workspace state appears in status as `requests`, `warm_hits`, `rebuilds`, `invalidations`,
`evictions`, and `cache_state`.

Metadata is stored in `.delphi-lsp/agent-cache/daemon.json` with owner-only token and
permissions (`daemon.json` mode 600 and parent 700). Do not copy or share this token
outside the root workspace.

`view --layer` accepts `overview`, `projects`, `units`, `unit`,
`symbols`, `symbol`, `implementation`, `references`, `problems`, and
`metrics`. For example, `delphi-lsp-agent view --layer metrics --format json`
returns a project summary and detailed unit metric objects; `--query` filters
units by name or path.
`index` materializes overview, projects, and problems JSON. `skill install`
writes the skill; `opencode install` writes the package-named skill, Markdown
agent, and plugin. The two deprecated write flags are harmless aliases and do
not change user configuration.
`worker` serves NDJSON over standard input/output.

Protocol v2 actions are `open`, `find`, `inspect`, `trace`, `focus`,
`problems`, and `metrics`. A `metrics` request without a query returns the
project summary followed by unit cards. A query filters units, while a unit
`target_id` from `open` selects one unit; `detail: "members"` adds routine,
Halstead, dependency, and symbol-count detail without returning source text.
Detail values are `summary`, `declaration`, `members`,
`context`, `body`, and `implementations`. Relations are `references`,
`callers`, `callees`, `uses`, `used_by`, `inherits`, and
`implements`.

A request requires `action` and can include `query`, `target_id`,
`project_id`, `detail`, `relation`, `cursor`, `max_items`, and
`max_chars`. Defaults are empty text fields, `detail: "summary"`, no
relation, `max_items: 12`, and `max_chars: 12000`. Ranges are 1–50 items
and 256–40000 characters. A successful envelope has `schema: 2`,
`workspace_revision`, `focus` (project, unit, and target IDs), `result`,
`page`, and `context`; errors have `schema: 2` and a code/message.

Focus preserves the selected project, unit, or target. Cursors bind a workspace
revision and request fingerprint, so source changes and cross-target or
cross-detail reuse invalidate them. `max_items` and `max_chars` bound each
response. A `sound_partial` relation is sound but incomplete: unresolved and
ambiguous relations are never fabricated. Unsupported relations are rejected.

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

The plugin maintains one worker per session/root, reusing focus and indexes.
During compaction it restores the focus and summary into the new context.
Transport failure, session deletion, and plugin disposal clean up the worker.

OpenCode history: 1.1.0 and 1.1.1 used a spawned view per call model.
Persistent session/root worker support first shipped in 2.0.0.
This is the same persistent session/root worker boundary.
The OpenCode worker stays separate from CLI daemon, and current plugin behavior is unchanged.

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
`open`, `find`, `focus`, and `inspect`. Use semantic tool calls, not raw
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
