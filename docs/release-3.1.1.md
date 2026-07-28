# Python Delphi LSP 3.1.1

Version 3.1.1 is a backwards-compatible correctness, robustness, and scaling
release. It remediates 38 verified defects across the parser, semantic index,
LSP server, cache daemon, agent protocol, project discovery, packaging, and
cross-platform process boundaries.

## Correctness and resilience

- Full-document synchronization prevents incremental editor changes from
  replacing complete buffers with deltas.
- Tolerant parsing preserves declarations after less-than comparisons,
  metaclass references, class helpers, incomplete enums, and nested routines.
- Include source maps now keep symbols, references, diagnostics, and rename
  edits attached to their original files and positions.
- All inbound and outbound LSP columns use UTF-16 code units.
- Malformed encodings and invalid project configuration degrade to structured
  problems or warnings instead of terminating indexing.
- Project discovery, workspace exclusions, symlink boundaries, target IDs,
  relative output paths, and UNC paths are handled consistently.

## Performance and bounded resources

- Document updates rebuild only changed semantic models.
- Workspace-symbol, prepared-response, source-line, and navigation caches are
  bounded and evict stale state.
- Workspace scans prune excluded directories before traversal.
- Metrics and knowledge export use streaming paths with bounded source caches.
- Repeated recursive-glob segments are collapsed before regex compilation.
- Cache-daemon clients are served concurrently through a bounded worker pool;
  silent unauthenticated connections cannot block authenticated requests.

## Verification

The release suite completes with `861 passed`, one platform-specific skip, and
60 subtests. Parser golden snapshots cover 45 fixture files: no symbol or
resolved-reference count decreased and no previously clean file began raising.
The package build, Twine validation, unpacked-sdist tests, wheel installation,
and PEP 561 marker check passed. GitHub CI passed on Linux, macOS, and Windows
with Python 3.10 and Python 3.14.

Install from PyPI:

```bash
python -m pip install --upgrade python-delphi-lsp==3.1.1
```
