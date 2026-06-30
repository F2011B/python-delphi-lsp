# Python Delphi LSP

Python Delphi LSP is a standalone Python package for Delphi/Object Pascal parsing,
semantic indexing, diagnostics, workspace symbol lookup, and Language Server
Protocol support.

The package is extracted from DelphiAST-oriented work and keeps the public Python
package name `delphiast` for parser and semantic APIs. The command-line language
server entrypoint is `delphi-lsp`.

## Features

- Delphi/Object Pascal parser for `.pas`, `.dpr`, and `.dpk` files
- Preprocessor support for include files, conditional defines, and compiler
  directives
- AST node model and XML-style writer
- Semantic model with units, types, members, references, and diagnostics
- Workspace indexing across multiple source files
- LSP features for hover, definition, references, rename, completion, document
  symbols, workspace symbols, and diagnostics

## Install

Runtime parser and semantic package:

```bash
python -m pip install python-delphi-lsp
```

Language server extras:

```bash
python -m pip install "python-delphi-lsp[lsp]"
```

Development and test extras from a checkout:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Usage

Parse Delphi source:

```python
from delphiast import parse

result = parse("unit Unit1; interface implementation end.")
print(result.root)
```

Start the language server over stdio:

```bash
delphi-lsp
```

## Repository Layout

- `delphiast/` - parser, preprocessor, AST, semantic model, workspace indexer,
  and LSP server implementation
- `tests/` - parser, semantic, workspace, diagnostics, and LSP tests
- `tests/fixtures/` - Delphi/Object Pascal test fixtures, including legacy
  DelphiAST snippets

## License

This project is licensed under the Mozilla Public License 2.0. See `LICENSE`.
