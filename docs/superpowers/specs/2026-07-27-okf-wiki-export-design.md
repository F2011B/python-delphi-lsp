# Open Knowledge Format Wiki Export

## Goal

Add a one-shot command that turns a Delphi repository into a portable,
cross-linked Markdown knowledge bundle:

```text
delphi-lsp-agent wiki export --root PATH --out PATH
```

The output conforms to Open Knowledge Format (OKF) 0.2. It contains the union
of information exposed by the layered codebase views without duplicating the
same data for every possible query spelling or pagination combination.

## Design

The exporter builds one deep `CodebaseIndex`, then writes:

- a root `index.md` with `okf_version: "0.2"`;
- overview and manifest concepts;
- one concept per project, unit, and non-unit symbol;
- focused source fragments for declarations and implementations;
- symbol-to-symbol, symbol-to-unit, and unit-to-project links;
- workspace and per-unit metrics;
- discovery and project-indexing problems;
- reference documentation for Protocol v3 actions, detail modes, relations,
  graph kinds, pagination, cache behavior, and CPG limitations.

The export represents unique indexed knowledge. Lazy CPG query results are not
eagerly expanded for every symbol: doing so would multiply parse work and
output size on multi-million-line repositories. The wiki documents those live
query surfaces and preserves each symbol's stable path/name/location identity.

Each concept file uses UTF-8 Markdown and YAML frontmatter with at least a
non-empty `type`. Directory `index.md` files provide progressive disclosure.
Names use readable slugs plus a stable digest, preventing collisions on
case-insensitive filesystems.

## Safety and determinism

Files are rendered in stable order and written to a sibling temporary
directory before the completed bundle replaces the destination. An existing
non-empty destination is rejected unless `--force` is present. A forced
replacement applies only to the exact resolved destination and refuses
symlinks, the repository root, and ancestors of the repository root.

No optional runtime dependency is required. YAML scalar values are emitted in
JSON-compatible syntax, which is valid YAML 1.2.

## Performance contract

- The semantic codebase index is built exactly once.
- Source files are read at most once for implementation fragments and once by
  the existing project metrics pass.
- Pages are streamed to disk rather than retained as one giant string.
- The existing parallel outline worker setting is exposed as `--workers`.
- No per-symbol CPG construction or Cartesian query expansion occurs.

## Release contract

This feature and the uncapped symbols-layer regression ship as version 3.1.0.
Release requires the full test suite, source/wheel checks, clean-install CLI
smoke tests, green GitHub CI, PyPI upload verification, a signed-off Git tag,
and a GitHub release.
