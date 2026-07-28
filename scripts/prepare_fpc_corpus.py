#!/usr/bin/env python3
"""Prepare a reproducible, FPC-only performance corpus outside the repository.

This is deliberately separate from ``build_github_performance_corpus.py``:
the mixed corpus is a broad compatibility fixture, while this corpus preserves
one coherent compiler tree and can be sliced reproducibly for scaling tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from build_github_performance_corpus import (
    LOCK_DEFAULT,
    MANIFEST_NAME,
    build_manifest,
    load_corpus_lock,
    ordered_source_files,
    prepare_cache_repo,
    select_source_round_robin,
    verify_manifest,
)


DEFAULT_TARGET_LINES = 3_000_000
DEFAULT_CACHE = Path(tempfile.gettempdir()) / "python-delphi-lsp-fpc-cache"
FPC_NAME = "FPCSource"


def _fpc_lock(lock_path: Path, target_lines: int) -> tuple[dict[str, Any], dict[str, Any]]:
    lock = load_corpus_lock(lock_path)
    matches = [corpus for corpus in lock["corpora"] if corpus["name"] == FPC_NAME]
    if len(matches) != 1:
        raise RuntimeError("Corpus lock must contain exactly one FPCSource entry.")
    fpc = dict(matches[0])
    if not fpc.get("anchors"):
        raise RuntimeError("FPCSource must define stable non-empty anchors in the corpus lock.")
    return {
        "schema_version": 1,
        "target_lines": target_lines,
        "vendor_corpora": False,
        "policy": lock.get("policy", ""),
        "corpora": [fpc],
    }, fpc


def _ensure_empty_workspace(workspace: Path) -> None:
    if workspace.exists() and any(workspace.iterdir()):
        raise RuntimeError(f"Workspace must be absent or empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def prepare_fpc_corpus(
    *,
    workspace: Path,
    lock_path: Path = LOCK_DEFAULT,
    cache_root: Path = DEFAULT_CACHE,
    target_lines: int = DEFAULT_TARGET_LINES,
    fetch: bool = False,
    offline: bool = False,
    checkout: Path | None = None,
) -> dict[str, Any]:
    """Build and verify a deterministic prefix of the pinned FPC source tree."""
    if target_lines < 2_000_000:
        raise ValueError("target_lines must be at least 2000000")
    workspace = workspace.expanduser().resolve()
    cache_root = cache_root.expanduser().resolve()
    lock_path = lock_path.expanduser().resolve()
    checkout = checkout.expanduser().resolve() if checkout is not None else None
    fpc_lock, fpc = _fpc_lock(lock_path, target_lines)

    cache_root.mkdir(parents=True, exist_ok=True)
    repo_root = prepare_cache_repo(
        fpc,
        cache_root,
        fetch=fetch,
        offline=offline,
        checkout_override=checkout,
    ).resolve()
    ordered = ordered_source_files(
        repo_root,
        anchors=list(fpc["anchors"]),
        paths=list(fpc["paths"]),
        extensions=list(fpc["extensions"]),
    )
    if not ordered:
        raise RuntimeError("No supported FPC source files were discovered.")
    selected, line_count = select_source_round_robin(
        {FPC_NAME: ordered},
        target_lines,
    )
    if line_count < target_lines:
        raise RuntimeError(
            f"Pinned FPC checkout has only {line_count} supported lines; "
            f"need {target_lines}."
        )

    _ensure_empty_workspace(workspace)
    for source in selected[FPC_NAME]:
        _link_or_copy(source, workspace / FPC_NAME / source.relative_to(repo_root))

    manifest = build_manifest(
        {FPC_NAME: ordered},
        selected,
        fpc_lock,
        workspace,
        cache_root,
        {FPC_NAME: repo_root},
    )
    manifest.update(
        {
            "target_reached": True,
            "selection": "anchors_then_casefolded_path_prefix",
            "source_scope": "FPCSource only; .pas and .inc",
        }
    )
    verify_manifest(workspace, manifest)
    manifest_path = workspace / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--lock", type=Path, default=LOCK_DEFAULT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--target-lines", type=int, default=DEFAULT_TARGET_LINES)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--checkout", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = prepare_fpc_corpus(
            workspace=args.workspace,
            lock_path=args.lock,
            cache_root=args.cache,
            target_lines=args.target_lines,
            fetch=args.fetch,
            offline=args.offline,
            checkout=args.checkout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"prepare failed: {exc}\n")
    print(
        f"Prepared {manifest['file_count']} FPC files and "
        f"{manifest['line_count']} lines at {manifest['workspace_root']}"
    )
    print(f"manifest: {Path(manifest['workspace_root']) / MANIFEST_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
