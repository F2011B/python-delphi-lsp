#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_DEFAULT = REPO_ROOT / "tests" / "corpora.performance.lock.json"
DEFAULT_CACHE = Path("/tmp/python-delphi-lsp-2m-cache")
DEFAULT_WORKSPACE = Path("/tmp/python-delphi-lsp-2m-workspace")
MANIFEST_NAME = "corpus-manifest.json"

ALLOWED_EXTENSIONS = {".pas", ".inc"}
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _line_count(path: Path) -> int:
    data = path.read_bytes()
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=None if cwd is None else str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )
    return (result.stdout or "").strip()


def _git_head(repo: Path) -> str:
    return _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()


def _git_status(repo: Path) -> str:
    return _run(["git", "-C", str(repo), "status", "--porcelain"]).strip()


def _git_fetch(repo: Path, revision: str) -> None:
    _run(["git", "-C", str(repo), "fetch", "--depth", "1", "origin", revision])


def _git_checkout(repo: Path, revision: str) -> None:
    _run(["git", "-C", str(repo), "checkout", "--quiet", revision])


def _git_clone(url: str, destination: Path) -> None:
    _run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(destination)])


def validate_lock_schema(lock: dict[str, Any]) -> None:
    if not isinstance(lock, dict):
        raise ValueError("Corpus lock must be a JSON object.")
    if lock.get("schema_version") != 1:
        raise ValueError("Corpus lock schema_version must be 1.")
    if not isinstance(lock.get("target_lines"), int) or lock["target_lines"] < 2_000_000:
        raise ValueError("Corpus lock target_lines must be at least 2000000.")
    if lock.get("vendor_corpora") is not False:
        raise ValueError("Corpus lock must explicitly disable vendoring.")
    corpora = lock.get("corpora")
    if not isinstance(corpora, list) or not corpora:
        raise ValueError("Corpus lock must define a non-empty corpora list.")

    names: set[str] = set()
    for index, corpus in enumerate(corpora):
        if not isinstance(corpus, dict):
            raise ValueError(f"Corpus entry {index} must be an object.")
        name = corpus.get("name")
        repository = corpus.get("repository")
        revision = corpus.get("revision")
        paths = corpus.get("paths")
        extensions = corpus.get("extensions")
        anchors = corpus.get("anchors", [])

        if not isinstance(name, str) or not name:
            raise ValueError(f"Corpus entry {index} must have a non-empty name.")
        if name in names:
            raise ValueError(f"Duplicate corpus name: {name!r}.")
        names.add(name)

        if not isinstance(repository, str) or not repository.startswith("https://github.com/"):
            raise ValueError(f"Corpus {name!r} must define an https://github.com URL.")
        if not SHA1_RE.fullmatch(str(revision or "")):
            raise ValueError(f"Corpus {name!r} revision must be a full 40-character SHA.")
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
            raise ValueError(f"Corpus {name!r} paths must be a non-empty string list.")
        if (
            not isinstance(extensions, list)
            or not extensions
            or not all(isinstance(item, str) and item in ALLOWED_EXTENSIONS for item in extensions)
        ):
            raise ValueError(f"Corpus {name!r} extensions must be supported scanner extensions.")
        if not isinstance(anchors, list) or not all(isinstance(item, str) for item in anchors):
            raise ValueError(f"Corpus {name!r} anchors must be a list of strings.")


def load_corpus_lock(path: Path | str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_lock_schema(payload)
    return payload


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _is_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS


def _visible_path(path: Path) -> bool:
    return ".git" not in path.parts


def _sort_key(path: Path, root: Path) -> tuple[str, str]:
    relative = path.relative_to(root)
    return (relative.parent.as_posix().casefold(), relative.name.casefold())


def _locked_path(root: Path, relative: str, *, label: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise RuntimeError(f"{label} escapes checkout: {relative}")
    if not candidate.exists():
        raise RuntimeError(f"{label} is missing: {relative}")
    return candidate


def _iter_source_files(
    root: Path,
    *,
    paths: list[str] | None = None,
    extensions: list[str] | None = None,
    anchors: list[str] | None = None,
) -> list[Path]:
    found: list[Path] = []
    root = root.resolve()
    paths = paths or ["."]
    extensions = extensions or sorted(ALLOWED_EXTENSIONS)
    anchors = anchors or []
    allowed = {extension.casefold() for extension in extensions}
    selected: set[Path] = set()

    def add(path: Path) -> None:
        path = path.resolve()
        if path in selected or path.suffix.casefold() not in allowed:
            return
        selected.add(path)
        found.append(path)

    for anchor in anchors:
        anchor_path = _locked_path(root, anchor, label="anchor")
        if not anchor_path.is_file() or anchor_path.suffix.casefold() not in allowed:
            raise RuntimeError(f"anchor is not a supported source file: {anchor}")
        add(anchor_path)

    candidates: dict[str, Path] = {}
    for relative in paths:
        locked = _locked_path(root, relative, label="path")
        values = (locked,) if locked.is_file() else locked.rglob("*")
        for candidate in values:
            if not candidate.is_file() or candidate.suffix.casefold() not in allowed:
                continue
            resolved = candidate.resolve()
            if not _visible_path(resolved.relative_to(root)):
                continue
            candidates[resolved.relative_to(root).as_posix()] = resolved
    for relative in sorted(candidates, key=lambda item: (item.casefold(), item)):
        add(candidates[relative])
    return found


def ordered_source_files(
    repo_root: Path,
    anchors: list[str] | None = None,
    *,
    paths: list[str] | None = None,
    extensions: list[str] | None = None,
) -> list[Path]:
    return _iter_source_files(repo_root, paths=paths, extensions=extensions, anchors=anchors)


def prepare_cache_repo(
    spec: dict[str, Any],
    cache_root: Path,
    *,
    fetch: bool,
    offline: bool,
    checkout_override: Path | None = None,
) -> Path:
    if offline and fetch:
        raise RuntimeError("--offline and --fetch are mutually exclusive.")

    name = str(spec["name"])
    repository_root = checkout_override or cache_root / name
    url = str(spec["repository"])
    revision = str(spec["revision"])

    if checkout_override is not None:
        if not _is_git_repo(repository_root):
            raise RuntimeError(f"Checkout override is not a git repo: {repository_root}")
        if _git_status(repository_root):
            raise RuntimeError(f"Repository {name!r} has local changes; clean or commit before continuing.")
        current = _git_head(repository_root)
        if current != revision:
            raise RuntimeError(
                f"Checkout override {name!r} is at {current[:8]} but lock requires {revision[:8]}."
            )
        return repository_root.resolve()

    if repository_root.exists():
        if not _is_git_repo(repository_root):
            raise RuntimeError(f"Cache path is not a git repo: {repository_root}")
    else:
        if not fetch:
            raise RuntimeError(f"Missing cache for {name!r}. Re-run with --fetch.")
        _git_clone(url, repository_root)
        _git_fetch(repository_root, revision)
        _git_checkout(repository_root, revision)

    if _git_status(repository_root):
        raise RuntimeError(f"Repository {name!r} has local changes; clean or commit before continuing.")

    current = _git_head(repository_root)
    if current != revision:
        if offline:
            raise RuntimeError(f"Repository {name!r} is at {current[:8]} but lock requires {revision[:8]}.")
        if not fetch:
            raise RuntimeError(
                f"Repository {name!r} is at {current[:8]} but lock requires {revision[:8]}; pass --fetch."
            )
        _git_fetch(repository_root, revision)
        _git_checkout(repository_root, revision)
        current = _git_head(repository_root)
        if current != revision:
            raise RuntimeError(f"Could not align {name!r} to {revision[:8]}.")

    return repository_root


def select_source_round_robin(
    ordered_by_corpus: dict[str, list[Path]],
    target_lines: int,
) -> tuple[dict[str, list[Path]], int]:
    selection: dict[str, list[Path]] = {name: [] for name in ordered_by_corpus}
    indexes: dict[str, int] = {name: 0 for name in ordered_by_corpus}
    corpus_names = sorted(ordered_by_corpus)
    total_lines = 0
    while total_lines < target_lines:
        progressed = False
        for name in corpus_names:
            corpus_paths = ordered_by_corpus[name]
            index = indexes[name]
            if index >= len(corpus_paths):
                continue
            selected_path = corpus_paths[index]
            selection[name].append(selected_path)
            indexes[name] += 1
            total_lines += _line_count(selected_path)
            progressed = True
            if total_lines >= target_lines:
                break
        if not progressed:
            break
    return {name: files for name, files in selection.items() if files}, total_lines


def _ensure_workspace(workspace: Path) -> None:
    if workspace.exists() and any(workspace.iterdir()):
        raise RuntimeError(f"Workspace must be absent or empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)


def _copy_or_link_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.unlink(missing_ok=True)
    except TypeError:
        if target.exists():
            target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def build_manifest(
    ordered_by_corpus: dict[str, list[Path]],
    selected_by_corpus: dict[str, list[Path]],
    lock: dict[str, Any],
    workspace: Path,
    cache_root: Path,
    repo_roots: dict[str, Path],
) -> dict[str, Any]:
    corpus_entries: list[dict[str, Any]] = []
    total_lines = 0
    total_files = 0

    for corpus in lock["corpora"]:
        name = str(corpus["name"])
        selected = selected_by_corpus.get(name, [])
        if not selected:
            continue

        files: list[dict[str, Any]] = []
        corpus_lines = 0
        repo_root = repo_roots[name]
        for source in selected:
            lines = _line_count(source)
            files.append(
                {
                    "path": f"{name}/{source.relative_to(repo_root).as_posix()}",
                    "sha256": _sha256(source),
                    "lines": lines,
                    "bytes": source.stat().st_size,
                }
            )
            corpus_lines += lines
            total_lines += lines
            total_files += 1

        corpus_entries.append(
            {
                "name": name,
                "repository": str(corpus["repository"]),
                "revision": str(corpus["revision"]),
                "anchors": list(corpus.get("anchors", [])),
                "line_count": corpus_lines,
                "file_count": len(selected),
                "files": files,
            }
        )

    return {
        "schema_version": 1,
        "target_lines": int(lock["target_lines"]),
        "line_count": total_lines,
        "file_count": total_files,
        "cache_root": str(cache_root.resolve()),
        "workspace_root": str(workspace.resolve()),
        "corpora": corpus_entries,
    }


def _publish_workspace(
    selected_by_corpus: dict[str, list[Path]],
    workspace: Path,
    repo_roots: dict[str, Path],
) -> None:
    for name, selected_files in selected_by_corpus.items():
        repo_root = repo_roots[name]
        target_root = workspace / name
        for source in selected_files:
            target = target_root / source.relative_to(repo_root)
            _copy_or_link_file(source, target)


def build_performance_corpus(
    lock_path: Path,
    cache_root: Path,
    workspace: Path,
    fetch: bool = False,
    offline: bool = False,
    checkout_overrides: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if offline and fetch:
        raise RuntimeError("--offline and --fetch are mutually exclusive.")
    lock = load_corpus_lock(lock_path)
    checkout_overrides = checkout_overrides or {}
    known_names = {str(corpus["name"]) for corpus in lock["corpora"]}
    unknown = set(checkout_overrides) - known_names
    if unknown:
        raise RuntimeError(f"Unknown checkout overrides: {sorted(unknown)}")
    cache_root.mkdir(parents=True, exist_ok=True)

    ordered_by_corpus: dict[str, list[Path]] = {}
    repo_roots: dict[str, Path] = {}
    for corpus in lock["corpora"]:
        name = str(corpus["name"])
        repo_root = prepare_cache_repo(
            corpus,
            cache_root,
            fetch=fetch,
            offline=offline,
            checkout_override=checkout_overrides.get(name),
        )
        repo_roots[name] = repo_root
        ordered_by_corpus[name] = ordered_source_files(
            repo_root,
            anchors=list(corpus.get("anchors", [])),
            paths=list(corpus["paths"]),
            extensions=list(corpus["extensions"]),
        )
        if not ordered_by_corpus[name]:
            raise RuntimeError(f"No source files discovered for corpus {name!r}.")

    selected_by_corpus, total_lines = select_source_round_robin(
        ordered_by_corpus,
        int(lock["target_lines"]),
    )
    if total_lines < int(lock["target_lines"]):
        raise RuntimeError(
            f"Pinned checkouts contain only {total_lines} supported lines; need {lock['target_lines']}."
        )
    _ensure_workspace(workspace)
    _publish_workspace(selected_by_corpus, workspace, repo_roots)
    manifest = build_manifest(
        ordered_by_corpus,
        selected_by_corpus,
        lock,
        workspace,
        cache_root,
        repo_roots,
    )
    manifest["target_reached"] = True
    verify_manifest(workspace, manifest)
    manifest_path = workspace / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        f"Selected {manifest['file_count']} files from {len(selected_by_corpus)} corpora "
        f"({manifest['line_count']}/{lock['target_lines']} lines)."
    )
    print(f"manifest: {manifest_path}")
    return manifest


def _checkout_argument(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("--checkout must use NAME=PATH")
    return name, Path(raw_path).expanduser().resolve()


def verify_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    target_lines = manifest.get("target_lines")
    recorded_lines = manifest.get("line_count")
    recorded_files = manifest.get("file_count")
    corpora = manifest.get("corpora")
    if not isinstance(target_lines, int) or not isinstance(recorded_lines, int):
        raise RuntimeError("Manifest mismatch: invalid line summary.")
    if not isinstance(recorded_files, int) or not isinstance(corpora, list):
        raise RuntimeError("Manifest mismatch: invalid file summary.")
    total_lines = 0
    total_files = 0
    for corpus in corpora:
        if not isinstance(corpus, dict) or not SHA1_RE.fullmatch(str(corpus.get("revision", ""))):
            raise RuntimeError("Manifest mismatch: invalid corpus record.")
        files = corpus.get("files")
        if not isinstance(files, list):
            raise RuntimeError("Manifest mismatch: invalid corpus files.")
        for record in files:
            if not isinstance(record, dict):
                raise RuntimeError("Manifest mismatch: invalid file record.")
            relative = record.get("path")
            expected_lines = record.get("lines")
            expected_hash = record.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected_lines, int) or not isinstance(expected_hash, str):
                raise RuntimeError("Manifest mismatch: invalid file metadata.")
            source = (workspace / relative).resolve()
            if not source.is_relative_to(workspace.resolve()):
                raise RuntimeError(f"Manifest mismatch: path escapes workspace: {relative}")
            if not source.is_file() or _line_count(source) != expected_lines or _sha256(source) != expected_hash:
                raise RuntimeError(f"Manifest mismatch: {relative}")
            total_lines += expected_lines
            total_files += 1
    if total_lines != recorded_lines or total_files != recorded_files or total_lines < target_lines:
        raise RuntimeError(
            f"Manifest mismatch: verified {total_files} files/{total_lines} lines, "
            f"recorded {recorded_files}/{recorded_lines}, target {target_lines}."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the pinned GitHub performance corpus.")
    parser.add_argument("--lock", type=Path, default=LOCK_DEFAULT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--checkout", action="append", default=[], type=_checkout_argument)
    args = parser.parse_args()

    try:
        build_performance_corpus(
            lock_path=args.lock,
            cache_root=args.cache,
            workspace=args.workspace,
            fetch=args.fetch,
            offline=args.offline,
            checkout_overrides=dict(args.checkout),
        )
    except Exception as exc:  # noqa: BLE001 - user-facing CLI logs.
        print(f"build failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
