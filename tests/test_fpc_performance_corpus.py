from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "tests" / "corpora.performance.lock.json"
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_fpc_corpus.py"
BENCHMARK_SCRIPT = ROOT / "scripts" / "benchmark_fpc_corpus.py"


def _modules(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    prepare = importlib.import_module("prepare_fpc_corpus")
    benchmark = importlib.import_module("benchmark_fpc_corpus")
    github_benchmark = importlib.import_module("benchmark_github_corpus")
    return prepare, benchmark, github_benchmark


def test_fpc_lock_is_pinned_and_has_stable_anchors() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    fpc = next(corpus for corpus in lock["corpora"] if corpus["name"] == "FPCSource")

    assert fpc["repository"] == "https://github.com/fpc/FPCSource.git"
    assert fpc["revision"] == "a8e7ad4e2f2f6d3bdc240850075d85e659a42ff8"
    assert fpc["paths"] == ["."]
    assert fpc["extensions"] == [".pas", ".inc"]
    assert fpc["anchors"]
    assert "compiler/compiler.pas" in fpc["anchors"]
    assert "rtl/inc/system.inc" in fpc["anchors"]


def test_slice_selection_is_deterministic_and_has_one_file_overshoot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    records = [
        {"path": "FPCSource/a.pas", "lines": 3, "sha256": "a" * 64},
        {"path": "FPCSource/b.inc", "lines": 4, "sha256": "b" * 64},
        {"path": "FPCSource/c.pas", "lines": 8, "sha256": "c" * 64},
    ]

    first = benchmark.select_slice(records, 6)
    second = benchmark.select_slice(list(records), 6)

    assert first == second
    assert first["line_count"] == 7
    assert first["line_count"] >= first["target_lines"]
    assert first["line_count"] - first["target_lines"] <= records[1]["lines"]
    assert first["file_count"] == 2
    assert len(first["file_list_sha256"]) == 64


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        (lambda size: size, "linear"),
        (lambda size: size * __import__("math").log2(size), "n_log_n"),
        (lambda size: size**2, "superlinear"),
    ],
)
def test_scaling_fit_classifies_growth(
    monkeypatch: pytest.MonkeyPatch,
    formula,
    expected: str,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    points = [
        {"line_count": size, "cold_wall_seconds": formula(size) / 1_000_000}
        for size in (250_000, 500_000, 1_000_000, 2_000_000, 3_000_000)
    ]

    result = benchmark.fit_scaling(points, metric="cold_wall_seconds")

    assert result["classification"] == expected
    assert result["exponent"] == pytest.approx(
        1.0 if expected == "linear" else (2.0 if expected == "superlinear" else result["exponent"]),
        rel=0.02,
    )


def test_parse_accounting_is_auditable(monkeypatch: pytest.MonkeyPatch) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    records = [
        {
            "path": "FPCSource/a.pas",
            "physical_lines": 100,
            "preprocessed_lines": 80,
            "status": "parsed_ok",
            "parser_mode": "lark",
            "problems": [],
        },
        {
            "path": "FPCSource/b.inc",
            "physical_lines": 50,
            "preprocessed_lines": 25,
            "status": "parsed_with_problems",
            "parser_mode": "delphiast",
            "problems": ["unresolved_include", "syntax"],
        },
        {
            "path": "FPCSource/c.pas",
            "physical_lines": 25,
            "preprocessed_lines": 0,
            "status": "failed",
            "parser_mode": "unknown",
            "problems": ["exception"],
        },
    ]

    accounting = benchmark.aggregate_parse_accounting(records)

    assert accounting["files_total"] == 3
    assert accounting["parsed_ok"] == 1
    assert accounting["parsed_with_problems"] == 1
    assert accounting["failed"] == 1
    assert accounting["accepted_files"] == 2
    assert accounting["physical_lines"] == 175
    assert accounting["preprocessed_lines"] == 105
    assert accounting["accepted_ratio"] == pytest.approx(2 / 3)
    assert accounting["problem_types"]["unresolved_include"] == 1
    assert accounting["common_problem_descriptions"] == []
    assert accounting["parser_modes"] == {"delphiast": 1, "lark": 1, "unknown": 1}
    assert accounting["top_problem_files"][0]["path"] == "FPCSource/b.inc"


def test_report_schema_is_stable_and_json_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    report = benchmark.assemble_report(
        workspace=Path("/tmp/fpc"),
        manifest={"line_count": 3_000_001, "file_count": 100},
        slices=[],
        parse_accounting={"accepted_ratio": 0.99},
        scaling={"exponent": 1.01, "classification": "linear"},
        phase_breakdown={},
        worker_sweep={},
        import_floor={},
        platform_info={"system": "test"},
        budgets=benchmark.DEFAULT_BUDGETS,
        projection={"factor": 4.0},
        observations=[],
    )

    assert set(report) == {
        "schema_version",
        "status",
        "product_question",
        "workspace",
        "manifest",
        "platform",
        "budgets",
        "instrumentation",
        "slices",
        "parse_accounting",
        "scaling",
        "phase_breakdown",
        "worker_sweep",
        "import_floor",
        "target_projection",
        "observations",
        "verdict",
        "failures",
    }
    assert json.loads(json.dumps(report, sort_keys=True)) == report


def _passing_report(benchmark) -> dict:
    return {
        "slices": [
            {
                "target_lines": 3_000_000,
                "line_count": 3_000_010,
                "cold_wall_seconds": 59.0,
                "warm_wall_seconds": 0.9,
                "peak_rss_bytes": 1_900_000_000,
            }
        ],
        "parse_accounting": {"accepted_ratio": 0.96},
        "scaling": {"exponent": 1.1},
        "budgets": dict(benchmark.DEFAULT_BUDGETS),
    }


def test_release_failures_accepts_passing_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)

    assert benchmark.release_failures(_passing_report(benchmark)) == []


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda report: report["slices"][0].update(cold_wall_seconds=61.0), "cold"),
        (lambda report: report["slices"][0].update(warm_wall_seconds=5.1), "warm"),
        (
            lambda report: report["slices"][0].update(
                peak_rss_bytes=12 * 1024**3
            ),
            "rss",
        ),
        (lambda report: report["parse_accounting"].update(accepted_ratio=0.94), "parse"),
        (lambda report: report["scaling"].update(exponent=1.16), "exponent"),
    ],
)
def test_each_release_budget_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    needle: str,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    report = _passing_report(benchmark)
    mutation(report)

    failures = benchmark.release_failures(report)

    assert len(failures) == 1
    assert needle in failures[0].casefold()


def test_fast_result_still_fails_parse_ratio_and_exponent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, benchmark, _ = _modules(monkeypatch)
    report = _passing_report(benchmark)
    report["slices"][0].update(cold_wall_seconds=1.0, warm_wall_seconds=0.01)
    report["parse_accounting"]["accepted_ratio"] = 0.4
    report["scaling"]["exponent"] = 1.8

    failures = benchmark.release_failures(report)

    assert len(failures) == 2
    assert any("parse" in failure.casefold() for failure in failures)
    assert any("exponent" in failure.casefold() for failure in failures)


def test_fpc_manifest_rejects_escape_and_sha_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare, _, _ = _modules(monkeypatch)
    workspace = tmp_path / "workspace"
    source = workspace / "FPCSource" / "compiler" / "compiler.pas"
    source.parent.mkdir(parents=True)
    source.write_text("unit compiler;\n", encoding="utf-8")
    record = {
        "path": "FPCSource/compiler/compiler.pas",
        "lines": 1,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    original_path = record["path"]
    manifest = {
        "schema_version": 1,
        "target_lines": 1,
        "line_count": 1,
        "file_count": 1,
        "corpora": [
            {
                "name": "FPCSource",
                "repository": "https://github.com/fpc/FPCSource.git",
                "revision": "a8e7ad4e2f2f6d3bdc240850075d85e659a42ff8",
                "files": [record],
            }
        ],
    }

    prepare.verify_manifest(workspace, manifest)
    manifest["corpora"][0]["files"][0]["path"] = "../escape.pas"
    with pytest.raises(RuntimeError, match="escapes"):
        prepare.verify_manifest(workspace, manifest)
    manifest["corpora"][0]["files"][0]["path"] = original_path
    manifest["corpora"][0]["files"][0]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="Manifest mismatch"):
        prepare.verify_manifest(workspace, manifest)


def test_fpc_benchmark_reuses_cross_platform_rss_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, benchmark, github_benchmark = _modules(monkeypatch)

    assert benchmark._peak_rss_bytes is github_benchmark._peak_rss_bytes


def test_fpc_performance_assets_are_packaged_and_perf_is_opt_in() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "include scripts/prepare_fpc_corpus.py" in manifest
    assert "include scripts/benchmark_fpc_corpus.py" in manifest
    assert "include tests/test_fpc_performance_corpus.py" in manifest
    assert "perf" in pyproject
    assert "not perf" in pyproject


@pytest.mark.perf
def test_smallest_real_fpc_slice() -> None:
    workspace = os.environ.get("DELPHI_LSP_FPC_CORPUS")
    if not workspace:
        pytest.skip("set DELPHI_LSP_FPC_CORPUS to a prepared corpus")
    sys.path.insert(0, str(ROOT / "scripts"))
    benchmark = importlib.import_module("benchmark_fpc_corpus")

    report = benchmark.run_benchmark(
        Path(workspace),
        targets=[250_000],
        parse_audit=False,
        run_worker_sweep=False,
    )

    assert report["schema_version"] == 1
    assert report["status"] in {"pass", "fail"}
    assert report["slices"][0]["line_count"] >= 250_000
    assert report["slices"][0]["cold_lines_per_second"] > 0
