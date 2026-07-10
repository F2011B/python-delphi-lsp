# Python Delphi LSP

`python-delphi-lsp` parses Delphi/Object Pascal, builds semantic and project
indexes, serves LSP, and provides bounded codebase navigation for agents.
Version 2.0.0 is authored by Dark Light and supports Windows, macOS, and Linux.

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
delphi-lsp-agent view --root PATH [--project-file FILE] --layer LAYER
                      [--query TEXT] [--format markdown|json] [--deep-projects]
delphi-lsp-agent index --root PATH [--project-file FILE] [--out FILE]
delphi-lsp-agent skill install [--target PATH] [--force]
delphi-lsp-agent opencode install [--target PATH] [--python PYTHON]
                                  [--force] [--write-config]
delphi-lsp-agent worker --root PATH [--project-file FILE]
```

`view --layer` accepts `overview`, `projects`, `units`, `unit`,
`symbols`, `symbol`, `implementation`, `references`, and `problems`.
`index` materializes overview, projects, and problems JSON. `skill install`
writes the skill; `opencode install` writes both integration files, while
`--write-config` additionally writes the restricted agent configuration.
`worker` serves NDJSON over standard input/output.

Protocol v2 actions are `open`, `find`, `inspect`, `trace`, `focus`,
and `problems`. Detail values are `summary`, `declaration`, `members`,
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
ambiguous relations are never fabricated.

For every source size the navigator builds an outline first, loads source detail
lazily for a selected target, and returns only selected fragments. Typed source
chunks are at most 6000 characters and also respect the response budget. This
optimization does not remove LSP functionality.

## OpenCode semantic navigator

Install the generated integration in a worktree:

```bash
delphi-lsp-agent opencode install --target . --write-config
```

It writes:

```text
.agents/skills/delphi-codebase-navigator/SKILL.md
.opencode/plugins/delphi_codebase.ts
```

The generated configuration enables only the named
`delphi-codebase-navigator` skill and `delphi_codebase`. It denies
`bash`, `read`, `glob`, `grep`, and `lsp`, along with edit/write and
other raw source tools. The skill is enabled. The installer does not use the
retired `.opencode/tools` path.

The plugin maintains one worker per session/root, reusing focus and indexes.
During compaction it restores the focus and summary into the new context.
Transport failure, session deletion, and plugin disposal clean up the worker.

A generated OpenCode agent looks like this; providers and unrelated agents stay
unchanged:

```json
{
  "agent": {
    "vllm-delphi-codebase": {
      "tools": {
        "delphi_codebase": true, "skill": true, "lsp": false,
        "bash": false, "read": false, "glob": false, "grep": false
      },
      "permission": {
        "delphi_codebase": "allow",
        "skill": {"*": "deny", "delphi-codebase-navigator": "allow"},
        "lsp": "deny"
      }
    }
  }
}
```

Select `vllm-delphi-codebase`, ask it to load
`delphi-codebase-navigator`, then use `delphi_codebase` actions such as
`open`, `find`, `focus`, and `inspect`. Use semantic tool calls, not raw
source tools.

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

The proof generates a 117,511-line project. The verifier requires `skill`, `open` (`Main.dpr` evidence), `find`, `focus`, and `inspect`, checks `MegaProc02500` and
`Value := Value + 40`, and forbids raw `bash`, `read`, `glob`, and `grep`.
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

The bundled automatic helper is not a cross-platform startup mechanism.

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
