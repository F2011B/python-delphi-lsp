# Repository-Default Delphi Navigation

## Goal

Opening a repository must be sufficient for every navigation query, even when
the repository contains many Delphi projects. Project selection is optional.

## Behavior

- A repository containing no project entry keeps the existing synthetic
  workspace behavior.
- A repository containing one `.dpr` or `.dpk` keeps the precise single-project
  behavior.
- A repository containing multiple `.dpr` or `.dpk` files automatically
  activates a synthetic repository project covering every supported Delphi
  source exactly once.
- The discovered concrete projects remain visible through `open` and can still
  be selected by `project_id` for compatibility.
- An explicit `.dpr` or `.dpk` selects that entry as before.
- An explicit `.dproj` resolves its `MainSource` and then selects the associated
  `.dpr` or `.dpk`, including the `.dproj` search paths, include paths, and
  defines.
- Cache startup and the OpenCode worker prewarm the active repository or
  project navigation index. `find`, `metrics`, and other queries never require
  project selection merely because several project entries exist.

## Performance and memory

Repository mode performs one source inventory and indexes each source once. It
does not build and merge every project graph, avoiding duplicated units and
project-count-proportional memory use. Existing retained-memory limits,
compaction, disk shards, and the 80-percent warning remain unchanged.

## Errors

An explicit `.dproj` must contain a valid `MainSource` that resolves to an
existing `.dpr` or `.dpk`. Invalid or missing entry metadata is reported as a
project discovery problem rather than indexing the XML file as Pascal.

## Validation

Tests cover direct queries in a multi-project repository, cache prewarming,
concrete-project compatibility, explicit `.dproj` resolution and compiler
settings, CLI help/documentation, and the quiet Windows daemon launch.
