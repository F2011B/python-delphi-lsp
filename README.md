# Python Delphi LSP

Python Delphi LSP is a standalone Python package for Delphi/Object Pascal
parsing, semantic indexing, diagnostics, and Language Server Protocol support.

The distributable package is named `python-delphi-lsp`. The import package keeps
the established `delphiast` name. The language-server executable is
`delphi-lsp`, and the agent-facing codebase navigator executable is
`delphi-lsp-agent`.

## What It Provides

- Parser support for `.pas`, `.dpr`, `.dpk`, and `.inc` files
- Delphi preprocessor handling for include files, conditionals, and compiler
  directives
- Semantic symbols for units, types, methods, fields, properties, variables,
  constants, and references
- Workspace indexing across Delphi projects
- LSP support for document symbols, workspace symbols, hover, definition,
  references, rename, completion, and diagnostics
- Automatic project path discovery from `.dpr`, `.dpk`, `.dproj`, `.cfg`, and
  `.dof` files
- opencode integration through the experimental LSP tool and an installable
  `.agents` skill with a dedicated codebase-inspection tool

## Installation

Install the package from a built distribution or from PyPI once published:

```bash
python -m pip install python-delphi-lsp
```

For development from a checkout:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Python API Example

```python
from delphiast import parse

result = parse("unit Unit1; interface implementation end.", "Unit1.pas")
print(result.root)
```

Enable semantic analysis when you need symbols or diagnostics:

```python
from delphiast import parse

source = """
unit Unit1;

interface

type
  TGreeter = class
  public
    procedure SayHello;
  end;

implementation

procedure TGreeter.SayHello;
begin
end;

end.
"""

result = parse(source, "Unit1.pas", build_semantic=True)
for symbol in result.semantic.symbols:
    print(symbol.name, symbol.kind)
```

## Language Server Usage

Start the LSP server over stdio:

```bash
delphi-lsp
```

From a checkout, the equivalent command is:

```bash
python -m delphiast.lsp_server
```

The server expects normal LSP JSON-RPC over stdio. Editors and tools should set
the workspace root to the Delphi project directory.

Auto-discovery reads `.dpr`, `.dpk`, `.dproj`, `.cfg`, and `.dof` files to
collect unit search paths, include paths, defines, direct `Unit in 'path.pas'`
references, and workspace source directories. Manual initialization options are
still accepted as overrides, but a normal project should not require users to
set include or source paths by hand.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "lsp": {
    "delphi": {
      "command": ["python", "-m", "delphiast.lsp_server"],
      "extensions": [".pas", ".dpr", ".dpk", ".inc"],
      "initialization": {
        "autoDiscoverPaths": true
      }
    }
  }
}
```

## Agent Codebase Navigator

Install the opencode skill and custom tool in a Delphi checkout:

```bash
delphi-lsp-agent opencode install --target . --write-config
```

This writes:

- `.agents/skills/delphi-codebase-navigator/SKILL.md`
- `.opencode/tools/delphi_codebase.ts`
- an optional `vllm-delphi-codebase` agent entry in `opencode.json`

The skill tells agents to inspect Delphi code through layered semantic views
instead of loading full source files. The custom opencode tool calls
`python -m delphiast.agent_cli` directly, so the agent does not need shell text
search or raw file-reading tools to understand the codebase.

Useful direct commands:

```bash
delphi-lsp-agent view --root . --layer overview
delphi-lsp-agent view --root . --layer projects
delphi-lsp-agent view --root . --layer unit --query Mega100kUnit
delphi-lsp-agent view --root . --layer symbols --query TWorker --format json
delphi-lsp-agent view --root . --layer problems
```

Available layers are `overview`, `projects`, `units`, `unit`, `symbols`,
`symbol`, `references`, and `problems`. The output includes file and line
citations, declarations, ownership, visibility, type information, and
dependency/problem summaries. Routine bodies are not emitted.

## opencode Usage

This repository includes an `opencode.json` that registers the Delphi LSP tool
and model aliases for local Ollama and vLLM endpoints.

To add the Delphi language server to another opencode project without the
agent tool, install the package in the Python environment used by opencode and
add an `lsp.delphi` entry to that project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "lsp": {
    "delphi": {
      "command": ["python", "-m", "delphiast.lsp_server"],
      "extensions": [".pas", ".dpr", ".dpk", ".inc"],
      "initialization": {
        "autoDiscoverPaths": true
      }
    }
  }
}
```

For checkout development, this repository's own `opencode.json` uses the local
virtual environment and sets `PYTHONPATH` so opencode loads the source tree
directly:

```json
{
  "lsp": {
    "delphi": {
      "command": [".venv/bin/python", "-m", "delphiast.lsp_server"],
      "extensions": [".pas", ".dpr", ".dpk", ".inc"],
      "env": {
        "PYTHONPATH": "."
      },
      "initialization": {
        "autoDiscoverPaths": true
      }
    }
  }
}
```

On Windows checkout development, replace `.venv/bin/python` with
`.venv\\Scripts\\python.exe`. If `python-delphi-lsp` is installed normally, the
portable `["python", "-m", "delphiast.lsp_server"]` command above works without
`PYTHONPATH`.

For normal local opencode work, use the Ollama alias with a larger context and
enable opencode's experimental LSP tool when starting opencode:

```bash
OPENCODE_EXPERIMENTAL_LSP_TOOL=true opencode run --dir . --model ollama/ornith-lspctx
```

On Windows PowerShell:

```powershell
$env:OPENCODE_EXPERIMENTAL_LSP_TOOL = "true"
opencode run --dir . --model ollama/ornith-lspctx
```

For large Delphi files, prefer LSP operations over reading the file into the
model prompt. The reduced `vllm-lsp` agent disables filesystem and shell tools
and leaves only the LSP tool enabled:

```bash
OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
python scripts/run_opencode_lsp_probe.py \
  --cwd output/mega_lsp_chain_project \
  --model vllm/ornith-lspctx \
  --agent vllm-lsp \
  --require-tool lsp.workspaceSymbol:MegaProc02500 \
  --forbid-tool bash --forbid-tool read --forbid-tool glob --forbid-tool grep \
  --forbid-tool edit --forbid-tool write --forbid-tool task \
  --forbid-tool webfetch --forbid-tool todowrite --forbid-tool skill \
  'Use only the Delphi LSP tool. In file Mega100kUnit.pas, run workspaceSymbol with filePath "Mega100kUnit.pas", line 1, character 1, and query "MegaProc02500".'
```

For an LSP-first edit proof, use `vllm-lsp-edit`. It permits the opencode edit
tool after LSP lookup while still forbidding shell and direct file-reading tools:

```bash
OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
python scripts/run_opencode_lsp_probe.py \
  --cwd output/mega_lsp_chain_project \
  --model vllm/ornith-lspctx \
  --agent vllm-lsp-edit \
  --require-tool lsp.workspaceSymbol:MegaProc02500 \
  --require-tool 'edit:Edit applied successfully' \
  --forbid-tool bash --forbid-tool read --forbid-tool glob --forbid-tool grep \
  --forbid-tool write --forbid-tool task --forbid-tool webfetch \
  --forbid-tool todowrite --forbid-tool skill \
  'Use LSP first, then edit the exact MegaProc02500 block.'
```

## Reproducing the vLLM opencode Test

The vLLM test is designed to prove that opencode can work on Delphi files larger
than the model context by calling LSP instead of loading the source file into the
prompt.

The test does the following:

1. Creates `output/mega_lsp_chain_project/Mega100kUnit.pas`, a generated Delphi
   unit with more than 100,000 lines and the symbol `MegaProc02500`.
2. Writes an opencode sandbox config with an absolute `delphi-lsp` command and
   `PYTHONPATH` pointing at the checkout.
3. Uses `vllm/ornith-lspctx` with the reduced `vllm-lsp` agent.
4. Requires a completed `lsp.workspaceSymbol` tool call that returns
   `MegaProc02500`.
5. Fails immediately if opencode calls `read`, `bash`, `glob`, `grep`, `edit`,
   `write`, `task`, `webfetch`, `todowrite`, or `skill`.

On macOS, start the local vLLM helper and run the proof:

```bash
scripts/bootstrap_vllm_opencode_test.sh --start-vllm
```

By default the vLLM helper is offline-only. It checks the Hugging Face cache and
does not download model shards. Pass `--allow-download` only when you explicitly
want the helper to fill missing cache files.

On Windows PowerShell, use an already running vLLM-compatible endpoint:

```powershell
.\scripts\bootstrap_vllm_opencode_test.ps1 -UseRunningServer
```

Use a custom endpoint when vLLM runs in WSL, Docker, or on another machine:

```powershell
.\scripts\bootstrap_vllm_opencode_test.ps1 -UseRunningServer -BaseUrl "http://127.0.0.1:8001/v1"
```

The macOS helper uses these defaults:

- `MODEL_ID=deepreinforce-ai/Ornith-1.0-9B`
- `SERVED_MODEL_NAME=ornith-vllm-metal`
- `MAX_MODEL_LEN=44352`
- `MAX_NUM_SEQS=1`
- `VLLM_METAL_MEMORY_FRACTION=0.97`
- `TOOL_CALL_PARSER=qwen3_xml`

The release evidence from the local proof recorded:

- default opencode request: 29,318 system-prompt characters and 10 tool schemas
- reduced LSP-only request: 8,978 system-prompt characters and 1 tool schema
- generated test unit: 117k lines
- GitHub corpus file: 14,309 lines
- `context_budget.status = "pass"`
- `goal_audit.status = "pass"`

## Reproducing the vLLM opencode Skill Test

The codebase skill proof validates the `.agents` skill and opencode custom tool
instead of only the raw LSP tool. It creates a sandbox Delphi project with:

- `Main.dpr`
- `Main.dproj`
- `src/Mega100kUnit.pas` with more than 100,000 lines
- `include/build.inc`
- `.agents/skills/delphi-codebase-navigator/SKILL.md`
- `.opencode/tools/delphi_codebase.ts`

Run it against the local Ornith vLLM endpoint:

```bash
python scripts/bootstrap_vllm_codebase_skill_test.py --use-running-server
```

Or let the macOS helper start vLLM without downloading model shards:

```bash
python scripts/bootstrap_vllm_codebase_skill_test.py --start-vllm
```

This skill probe defaults the vLLM helper to `MAX_MODEL_LEN=32768`, which was
the stable local Metal setting for the complete skill + `delphi_codebase` tool
call. Override with `--max-model-len` when your machine has enough headroom.

The probe requires opencode to load `delphi-codebase-navigator`, call
`delphi_codebase`, and find `MegaProc02500`. It fails if opencode uses `bash`,
`read`, `glob`, `grep`, `edit`, `write`, `task`, `webfetch`, or `todowrite`
before the required evidence is complete.

## Verification

Run the local test suite:

```bash
python -m pytest -q
```

Generate the Delphi language-feature matrix:

```bash
python scripts/audit_delphi_language_features.py
```

Build and check distributable artifacts:

```bash
python -m build
python -m twine check dist/*
```

## Repository Layout

- `delphiast/` - parser, preprocessor, project discovery, semantic model,
  workspace indexer, agent layers, and LSP server
- `scripts/` - release evidence, cache checks, opencode probes, and bootstrap
  helpers
- `tests/` - parser, semantic, workspace, diagnostics, packaging, and LSP tests
- `tests/fixtures/` - Delphi/Object Pascal fixtures and legacy DelphiAST snippets

## License

This project is licensed under the Mozilla Public License 2.0. See
[`LICENSE`](LICENSE).
