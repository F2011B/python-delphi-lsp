from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "tests" / "corpora.performance.lock.json"
BUILD_SCRIPT = ROOT / "scripts" / "build_github_performance_corpus.py"
BENCH_SCRIPT = ROOT / "scripts" / "benchmark_github_corpus.py"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_has_schema_and_required_corpora() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    build = _load_module(BUILD_SCRIPT)

    build.validate_lock_schema(lock)
    assert lock["schema_version"] == 1
    assert lock["target_lines"] == 2_000_000

    corpora = {entry["name"]: entry for entry in lock["corpora"]}
    expected = {
        "mormot2": "58b4e9a8ca1e292d6beb89bb3ad05d3826f314f6",
        "DUnitX": "dca41c181b1a7a8d4c45c51ed6946da6d326d293",
        "DelphiAST": "38402535ad6018b981f08920836aac99b554cb86",
        "python4delphi": "d02e0fbecc65c104837ae1f103148044ef6e6f61",
        "FPCSource": "a8e7ad4e2f2f6d3bdc240850075d85e659a42ff8",
        "castle-engine": "a28b45bc1bd9f6bda7376e83a34bf71eea45b023",
    }
    assert set(corpora.keys()) >= set(expected.keys())
    for name, revision in expected.items():
        assert corpora[name]["revision"] == revision
        assert corpora[name]["repository"].startswith("https://github.com/")
        assert corpora[name]["paths"]
        assert set(corpora[name]["extensions"]) <= {".pas", ".inc"}
    assert lock["vendor_corpora"] is False
    assert corpora["FPCSource"]["anchors"] == []


def test_deterministic_round_robin_selection_is_stable(tmp_path: Path) -> None:
    build = _load_module(BUILD_SCRIPT)
    corpus_root = tmp_path / "cache"
    corpus_root.mkdir()
    alpha = corpus_root / "alpha"
    beta = corpus_root / "beta"
    for root in (alpha, beta):
        root.mkdir()
    for index, name in enumerate(["a1.pas", "a2.pas", "a3.pas"], start=1):
        (alpha / name).write_text(f"// {name}\n" * index, encoding="utf-8")
    for index, name in enumerate(["b1.pas", "b2.pas"], start=1):
        (beta / name).write_text(f"// {name}\n" * index, encoding="utf-8")

    ordered = {
        "alpha": build.ordered_source_files(alpha),
        "beta": build.ordered_source_files(beta),
    }
    selected, total = build.select_source_round_robin(ordered, target_lines=3)
    selected_names = {key: [path.name for path in paths] for key, paths in selected.items()}
    assert total >= 3
    assert selected_names["alpha"] == ["a1.pas", "a2.pas"]
    assert selected_names["beta"] == ["b1.pas"]


def test_unsupported_extensions_are_ignored_by_builder(tmp_path: Path) -> None:
    build = _load_module(BUILD_SCRIPT)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "valid.pas").write_text("a", encoding="utf-8")
    (repo / "valid.inc").write_text("a", encoding="utf-8")
    (repo / "invalid.pkp").write_text("a", encoding="utf-8")
    (repo / "invalid.dpr").write_text("a", encoding="utf-8")
    discovered = build.ordered_source_files(repo)
    discovered_names = {path.name for path in discovered}
    assert {"valid.pas", "valid.inc"} == discovered_names
    assert "invalid.dpr" not in discovered_names
    assert "invalid.pkp" not in discovered_names


def test_verify_manifest_rejects_path_escapes_and_tampering(tmp_path: Path) -> None:
    build = _load_module(BUILD_SCRIPT)
    workspace = tmp_path / "workspace"
    corpus = workspace / "mormot2"
    corpus.mkdir(parents=True)
    source = corpus / "src" / "unit.pas"
    source.parent.mkdir(parents=True)
    source.write_text("unit Bad;\\n", encoding="utf-8")
    manifest_path = workspace / "corpus-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_lines": 1,
                "corpora": [
                    {
                        "name": "mormot2",
                        "repository": "https://github.com/synopse/mORMot2.git",
                        "revision": "58b4e9a8ca1e292d6beb89bb3ad05d3826f314f6",
                        "files": [
                            {
                                "path": "../outside.pas",
                                "sha256": "deadbeef",
                                "lines": 1,
                            }
                        ],
                    }
                ],
                "line_count": 1,
                "file_count": 1,
                "workspace_root": str(workspace),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="path escapes"):
        build.verify_manifest(workspace, json.loads(manifest_path.read_text(encoding="utf-8")))

    correct_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_lines": 1,
                "corpora": [
                    {
                        "name": "mormot2",
                        "repository": "https://github.com/synopse/mORMot2.git",
                        "revision": "58b4e9a8ca1e292d6beb89bb3ad05d3826f314f6",
                        "files": [
                            {
                                "path": "mormot2/src/unit.pas",
                                "sha256": correct_sha[:8] + "00",
                                "lines": 1,
                            }
                        ],
                    }
                ],
                "line_count": 1,
                "file_count": 1,
                "workspace_root": str(workspace),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Manifest mismatch"):
        build.verify_manifest(workspace, json.loads(manifest_path.read_text(encoding="utf-8")))


def test_missing_locked_anchor_is_release_blocking(tmp_path: Path) -> None:
    build = _load_module(BUILD_SCRIPT)
    with pytest.raises(RuntimeError, match="anchor is missing"):
        build.ordered_source_files(tmp_path, anchors=["missing.pas"])


def test_builder_refuses_to_clear_a_nonempty_workspace(tmp_path: Path) -> None:
    build = _load_module(BUILD_SCRIPT)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "owned.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeError, match="absent or empty"):
        build._ensure_workspace(workspace)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_benchmark_budget_checks() -> None:
    benchmark = _load_module(BENCH_SCRIPT)
    passing = {
        "manifest": {"line_count": 2_000_000},
        "timings_seconds": {"open": 5.0, "cold_find": 20.0, "warm_find": 0.2},
    }
    assert benchmark.release_failures(passing) == []

    failing = {
        "manifest": {"line_count": 1_999_999},
        "timings_seconds": {"open": 10.0, "cold_find": 51.0, "warm_find": 1.1},
    }
    failures = benchmark.release_failures(failing)
    assert len(failures) == 3
    assert any("2000000" in item for item in failures)
    assert any("cold" in item for item in failures)
    assert any("warm" in item for item in failures)
