# Python Delphi LSP 3.1.0

Version 3.1.0 adds a complete Markdown knowledge export and fixes truncated
symbol-layer results.

## Open Knowledge Format wiki

The new command exports one repository as an Open Knowledge Format 0.2 bundle:

```bash
delphi-lsp-agent wiki export --root PATH --out codebase-wiki
```

The bundle contains a progressive-disclosure `index.md`, linked project, unit,
symbol, reference, problem, and metric concepts, declaration/implementation
source fragments, and reference pages for Protocol v3, relations, CPG, cache,
and live-query semantics.

Every concept uses UTF-8 Markdown and YAML frontmatter with a non-empty `type`.
Stable digest suffixes prevent filename collisions between overloads and equal
names. All internal Markdown links are validated by the test suite.

Generation is deterministic and uses the existing parallel outline path. Pages
are streamed to disk, the decoded-source cache retains at most four files, and
metrics process source paths without retaining the complete source corpus. The
complete bundle is installed only after successful generation. Existing
non-empty output requires `--force`; repository-root, ancestor, and symlink
destinations are rejected.

Lazy CPG results remain focused live queries. Eagerly expanding every
overlapping graph target would multiply parsing and make output unbounded on a
four-million-line codebase, so the wiki exports the graph/query contract plus
the indexed knowledge needed to select a target.

## Complete symbols layer

`view --layer symbols` now returns all symbols that match the query. The former
silent 200-item slice has been removed, and a regression fixture proves that
more than 200 symbols remain visible.

## Installation

PyPI:

```bash
python -m pip install --upgrade python-delphi-lsp==3.1.0
```
