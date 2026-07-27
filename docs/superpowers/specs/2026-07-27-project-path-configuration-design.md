# TOML Project Path Configuration Design

## Goal

Allow a Delphi monorepo to define which `.dpr`, `.dpk`, and `.dproj`
projects belong to the navigable codebase, and which project paths must never
be indexed.

## Configuration

Discovery automatically reads `.delphi-lsp.toml` from the repository root.
The supported schema is:

```toml
[projects]
include = [
  "apps/**",
  "services/Api/ApiServer.dproj",
]
exclude = [
  "**/examples/**",
  "legacy/**",
]
```

Patterns are repository-relative, case-insensitive, and use `/` separators.
An exact directory includes or excludes its descendants. `*` matches within
one path segment and `**` crosses directory boundaries. Absolute paths and
parent traversal (`..`) are invalid.

Both project entry files and their companion `.dproj` files participate in
matching. This lets a `.dproj` path select the `.dpr` or `.dpk` named by its
`MainSource`.

## Selection Rules

1. An explicit `--project-file` remains authoritative and bypasses configured
   include/exclude filters.
2. If `[projects]` contains `include` or `exclude`, discovery starts with all
   repository project candidates.
3. A non-empty `include` list keeps only matching candidates.
4. `exclude` is applied last and always wins.
5. Configured selection does not apply the built-in shallow-main-project
   heuristic; every remaining candidate is a selected main project.
6. If the configured selection matches no project, discovery fails with a
   precise error. It must not fall back to parsing all repository sources.
7. Without configured project filters, existing discovery behavior is
   unchanged.

The configuration applies at the shared discovery layer, so wiki export,
cache/worker navigation, index, view, and language-server discovery agree on
the project set.

## Parsing and Compatibility

Python 3.11+ uses `tomllib`; Python 3.10 uses the conditional `tomli`
dependency. Invalid TOML, invalid value types, unsupported `[projects]` keys,
unsafe patterns, and unmatched configured selections produce actionable
configuration errors that include the configuration path.

The configuration file is included in discovery metadata so cache revision
fingerprints change when project selection changes.

## Testing

Tests cover:

- multiple included monorepo projects at different depths;
- exclusion precedence, including `DUnitXExamples`;
- matching by `.dproj` path;
- explicit project override;
- missing matches without all-source fallback;
- malformed TOML and unsafe patterns;
- automatic application to workspace/cache discovery;
- unchanged behavior when no configuration exists.

