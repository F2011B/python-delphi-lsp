#!/usr/bin/env python3
"""Benchmark the real FPC compiler corpus through the agent ``view`` CLI path."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
import hashlib
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from benchmark_github_corpus import _peak_rss_bytes  # noqa: E402
from build_github_performance_corpus import (  # noqa: E402
    LOCK_DEFAULT,
    MANIFEST_NAME,
    _line_count,
    load_corpus_lock,
    verify_manifest,
)


DEFAULT_TARGETS = (250_000, 500_000, 1_000_000, 2_000_000, 3_000_000)
DEFAULT_BUDGETS = {
    "max_3m_cold_seconds": 60.0,
    "max_3m_warm_seconds": 5.0,
    "max_peak_rss_bytes": 11 * 1024**3,
    "min_parse_accepted_ratio": 0.95,
    "max_scaling_exponent": 1.15,
}
DEFAULT_REPORT = (
    Path(tempfile.gettempdir()) / "python-delphi-lsp-fpc-performance-report.json"
)
PRODUCT_QUESTION = (
    "Can python-delphi-lsp index three million lines of real FPC compiler "
    "Pascal on a standard Windows Core i5 PC without pathological scaling?"
)


def _number(value: object, default: float = math.inf) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _file_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    corpora = manifest.get("corpora")
    if not isinstance(corpora, list) or len(corpora) != 1:
        raise RuntimeError("FPC benchmark manifest must contain exactly one corpus.")
    corpus = corpora[0]
    if not isinstance(corpus, dict) or corpus.get("name") != "FPCSource":
        raise RuntimeError("FPC benchmark manifest must contain FPCSource only.")
    records = corpus.get("files")
    if not isinstance(records, list) or not records:
        raise RuntimeError("FPC benchmark manifest has no source file records.")
    return records


def _file_list_digest(records: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["lines"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def select_slice(records: list[dict[str, Any]], target_lines: int) -> dict[str, Any]:
    """Select a deterministic manifest prefix, with at most one-file overshoot."""
    if target_lines <= 0:
        raise ValueError("target_lines must be positive")
    selected: list[dict[str, Any]] = []
    total = 0
    for record in records:
        lines = record.get("lines")
        if not isinstance(lines, int) or isinstance(lines, bool) or lines < 0:
            raise ValueError(f"invalid line count in record: {record!r}")
        selected.append(record)
        total += lines
        if total >= target_lines:
            break
    if total < target_lines:
        raise RuntimeError(
            f"Corpus contains only {total} selectable lines; need {target_lines}."
        )
    return {
        "target_lines": target_lines,
        "line_count": total,
        "file_count": len(selected),
        "file_list_sha256": _file_list_digest(selected),
        "records": selected,
    }


def fit_scaling(
    points: list[dict[str, Any]],
    *,
    metric: str = "cold_wall_seconds",
) -> dict[str, Any]:
    """Fit ``time = c * lines**p`` in log space and classify exponent ``p``."""
    values = [
        (float(point["line_count"]), _number(point.get(metric)))
        for point in points
        if _number(point.get(metric)) > 0
        and math.isfinite(_number(point.get(metric)))
        and _number(point.get("line_count")) > 0
    ]
    if len(values) < 2:
        return {
            "metric": metric,
            "exponent": None,
            "coefficient": None,
            "r_squared": None,
            "classification": "insufficient_data",
            "point_count": len(values),
        }
    xs = [math.log(lines) for lines, _ in values]
    ys = [math.log(seconds) for _, seconds in values]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((value - mean_x) ** 2 for value in xs)
    exponent = sum(
        (x_value - mean_x) * (y_value - mean_y)
        for x_value, y_value in zip(xs, ys)
    ) / variance
    intercept = mean_y - exponent * mean_x
    predictions = [intercept + exponent * value for value in xs]
    total_variance = sum((value - mean_y) ** 2 for value in ys)
    residual = sum(
        (actual - predicted) ** 2
        for actual, predicted in zip(ys, predictions)
    )
    r_squared = 1.0 - residual / total_variance if total_variance else 1.0
    if exponent <= 1.04:
        classification = "linear"
    elif exponent <= 1.25:
        classification = "n_log_n"
    else:
        classification = "superlinear"
    return {
        "metric": metric,
        "exponent": exponent,
        "coefficient": math.exp(intercept),
        "r_squared": r_squared,
        "classification": classification,
        "point_count": len(values),
    }


def aggregate_parse_accounting(records: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(record.get("status", "failed")) for record in records)
    problem_types: Counter[str] = Counter()
    parser_modes: Counter[str] = Counter()
    descriptions: Counter[str] = Counter()
    top_problem_files: list[dict[str, Any]] = []
    for record in records:
        problems = [str(problem) for problem in record.get("problems", [])]
        problem_types.update(problems)
        descriptions.update(
            str(description)
            for description in record.get("problem_descriptions", [])
        )
        parser_modes[str(record.get("parser_mode", "unknown"))] += 1
        if problems:
            top_problem_files.append(
                {
                    "path": str(record.get("path", "")),
                    "problem_count": len(problems),
                    "problem_types": dict(Counter(problems)),
                }
            )
    top_problem_files.sort(
        key=lambda item: (-int(item["problem_count"]), str(item["path"]).casefold())
    )
    accepted = (
        status_counts["parsed_ok"] + status_counts["parsed_with_problems"]
    )
    total = len(records)
    common_descriptions = [
        {"description": description, "count": count}
        for description, count in sorted(
            descriptions.items(),
            key=lambda item: (-item[1], item[0].casefold(), item[0]),
        )[:10]
    ]
    return {
        "files_total": total,
        "parsed_ok": status_counts["parsed_ok"],
        "parsed_with_problems": status_counts["parsed_with_problems"],
        "failed": status_counts["failed"],
        "files_parsed_ok": status_counts["parsed_ok"],
        "files_with_problems": status_counts["parsed_with_problems"],
        "files_failed": status_counts["failed"],
        "accepted_files": accepted,
        "accepted_ratio": accepted / total if total else 0.0,
        "parse_success_ratio": accepted / total if total else 0.0,
        "physical_lines": sum(int(record.get("physical_lines", 0)) for record in records),
        "preprocessed_lines": sum(
            int(record.get("preprocessed_lines", 0)) for record in records
        ),
        "post_preprocessing_lines": sum(
            int(record.get("preprocessed_lines", 0)) for record in records
        ),
        "problem_types": dict(sorted(problem_types.items())),
        "syntax_problems_by_type": dict(sorted(problem_types.items())),
        "common_problem_descriptions": common_descriptions,
        "parser_modes": dict(sorted(parser_modes.items())),
        "top_problem_files": top_problem_files[:10],
    }


def release_failures(report: dict[str, Any]) -> list[str]:
    budgets = report.get("budgets", DEFAULT_BUDGETS)
    slices = report.get("slices", [])
    largest = max(
        (
            item
            for item in slices
            if isinstance(item, dict) and _number(item.get("target_lines"), 0) >= 3_000_000
        ),
        key=lambda item: _number(item.get("line_count"), 0),
        default=None,
    )
    failures: list[str] = []
    if largest is None:
        failures.append("missing required 3000000-line release slice")
    else:
        if _number(largest.get("cold_wall_seconds")) > _number(
            budgets.get("max_3m_cold_seconds")
        ):
            failures.append("cold 3M overview exceeds the release budget")
        if _number(largest.get("warm_wall_seconds")) > _number(
            budgets.get("max_3m_warm_seconds")
        ):
            failures.append("warm 3M overview exceeds the release budget")
        max_rss = max(
            (_number(item.get("peak_rss_bytes")) for item in slices),
            default=math.inf,
        )
        if max_rss > _number(budgets.get("max_peak_rss_bytes")):
            failures.append("peak RSS exceeds the release budget")
    parse_ratio = _number(
        report.get("parse_accounting", {}).get("accepted_ratio"), -math.inf
    )
    if parse_ratio < _number(budgets.get("min_parse_accepted_ratio")):
        failures.append("parse acceptance ratio is below the release budget")
    exponent = _number(report.get("scaling", {}).get("exponent"))
    if exponent > _number(budgets.get("max_scaling_exponent")):
        failures.append("scaling exponent exceeds the release budget")
    return failures


def _verdict(
    slices: list[dict[str, Any]],
    projection: dict[str, Any],
    budgets: dict[str, Any],
    platform_info: dict[str, Any],
) -> str:
    largest = max(
        (
            item
            for item in slices
            if _number(item.get("target_lines"), 0) >= 3_000_000
        ),
        key=lambda item: _number(item.get("line_count"), 0),
        default=None,
    )
    if largest is None or largest.get("cold_timed_out"):
        return "No: the required 3,000,000-line cold overview did not complete."
    cold = _number(largest.get("cold_wall_seconds"))
    budget = _number(budgets.get("max_3m_cold_seconds"))
    measured_answer = "Yes" if cold <= budget else "No"
    projected = _number(
        projection.get("projected_largest_cold_seconds"),
        math.nan,
    )
    projection_answer = "within" if projected <= budget else "outside"
    return (
        f"{measured_answer} on the measured {platform_info.get('system', 'unknown')} "
        f"{platform_info.get('machine', 'host')}: the cold 3M overview took "
        f"{cold:.3f}s versus {budget:.1f}s; the Windows i5 projection is "
        f"{projected:.3f}s ({projection_answer} budget) using the explicitly "
        f"assumed {projection.get('factor')}x de-rating, not a target measurement."
    )


def assemble_report(
    *,
    workspace: Path,
    manifest: dict[str, Any],
    slices: list[dict[str, Any]],
    parse_accounting: dict[str, Any],
    scaling: dict[str, Any],
    phase_breakdown: dict[str, Any],
    worker_sweep: dict[str, Any],
    import_floor: dict[str, Any],
    platform_info: dict[str, Any],
    budgets: dict[str, Any],
    projection: dict[str, Any],
    observations: list[str],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "product_question": PRODUCT_QUESTION,
        "workspace": str(workspace),
        "manifest": manifest,
        "platform": platform_info,
        "budgets": budgets,
        "instrumentation": {
            "cold": "fresh process after removal of the slice .delphi-lsp directory",
            "warm": "fresh process without cache removal; OS filesystem cache may be warm",
            "command": "delphi-lsp-agent view --layer overview --format json",
            "cpu": (
                "CLI parent-process CPU time for the operation; spawned worker "
                "CPU time is not aggregated"
            ),
            "rss": "peak RSS of the CLI parent process; worker RSS is not aggregated",
        },
        "slices": slices,
        "parse_accounting": parse_accounting,
        "scaling": scaling,
        "phase_breakdown": phase_breakdown,
        "worker_sweep": worker_sweep,
        "import_floor": import_floor,
        "target_projection": projection,
        "observations": observations,
        "verdict": _verdict(slices, projection, budgets, platform_info),
        "failures": [],
    }
    failures = release_failures(report)
    report["failures"] = failures
    report["status"] = "fail" if failures else "pass"
    return report


def _load_verified_manifest(
    workspace: Path,
    lock_path: Path,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    manifest_path = workspace / MANIFEST_NAME
    try:
        manifest: dict[str, Any] = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read corpus manifest {manifest_path}: {exc}") from exc
    verify_manifest(workspace, manifest)
    lock = load_corpus_lock(lock_path)
    fpc = next(
        (corpus for corpus in lock["corpora"] if corpus["name"] == "FPCSource"),
        None,
    )
    recorded = manifest["corpora"][0]
    if fpc is None or (
        recorded.get("repository"),
        recorded.get("revision"),
    ) != (fpc.get("repository"), fpc.get("revision")):
        raise RuntimeError("FPC manifest repository or revision differs from the lock.")
    return manifest_path, manifest, _file_records(manifest)


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _materialize_slice(
    workspace: Path,
    selected: dict[str, Any],
    destination: Path,
) -> None:
    for record in selected["records"]:
        relative = Path(str(record["path"]))
        source = (workspace / relative).resolve()
        target = destination / relative
        _link_or_copy(source, target)


def _clear_slice_cache(slice_root: Path) -> None:
    cache = (slice_root / ".delphi-lsp").resolve()
    if cache.parent != slice_root.resolve():
        raise RuntimeError(f"Refusing unsafe cache path: {cache}")
    if cache.exists():
        shutil.rmtree(cache)
    if cache.exists():
        raise RuntimeError(f"Could not clear cold-run cache: {cache}")


def _run_overview_process(
    slice_root: Path,
    workers: int,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_internal-overview",
        str(slice_root),
        str(workers),
    ]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "wall_seconds": time.perf_counter() - started,
            "cpu_seconds": None,
            "peak_rss_bytes": None,
            "returncode": None,
            "timed_out": True,
            "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
        }
    wall = time.perf_counter() - started
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        envelope = {}
    return {
        "wall_seconds": wall,
        "cpu_seconds": envelope.get("cpu_seconds"),
        "peak_rss_bytes": envelope.get("peak_rss_bytes"),
        "returncode": result.returncode,
        "timed_out": False,
        "overview": envelope.get("overview"),
        "stderr": result.stderr[-2000:],
    }


def _benchmark_slice(
    workspace: Path,
    selection: dict[str, Any],
    workers: int,
    *,
    timeout_seconds: float,
    skip_warm: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="delphi-lsp-fpc-slice-") as raw_temp:
        slice_root = Path(raw_temp).resolve()
        _materialize_slice(workspace, selection, slice_root)
        _clear_slice_cache(slice_root)
        cold = _run_overview_process(
            slice_root,
            workers,
            timeout_seconds=timeout_seconds,
        )
        cache_after_cold = (slice_root / ".delphi-lsp").exists()
        warm = (
            {}
            if skip_warm
            else _run_overview_process(
                slice_root,
                workers,
                timeout_seconds=timeout_seconds,
            )
        )
        cache_after_warm = (slice_root / ".delphi-lsp").exists()
    rss_values = [
        value
        for value in (cold.get("peak_rss_bytes"), warm.get("peak_rss_bytes"))
        if isinstance(value, int)
    ]
    overview = cold.get("overview")
    cold_wall = float(cold["wall_seconds"])
    warm_wall = _number(warm.get("wall_seconds"), math.nan)
    return {
        "target_lines": selection["target_lines"],
        "line_count": selection["line_count"],
        "file_count": selection["file_count"],
        "file_list_sha256": selection["file_list_sha256"],
        "workers": workers,
        "cold_wall_seconds": cold_wall,
        "cold_cpu_seconds": cold["cpu_seconds"],
        "warm_wall_seconds": warm.get("wall_seconds"),
        "warm_cpu_seconds": warm.get("cpu_seconds"),
        "cold_lines_per_second": (
            selection["line_count"] / cold_wall if cold_wall > 0 else None
        ),
        "warm_lines_per_second": (
            selection["line_count"] / warm_wall
            if math.isfinite(warm_wall) and warm_wall > 0
            else None
        ),
        "peak_rss_bytes": max(rss_values) if rss_values else None,
        "cold_returncode": cold["returncode"],
        "warm_returncode": warm.get("returncode"),
        "cold_timed_out": cold["timed_out"],
        "warm_timed_out": warm.get("timed_out"),
        "persistent_cache_after_cold": cache_after_cold,
        "persistent_cache_after_warm": cache_after_warm,
        "overview_source_count": (
            overview.get("source_count") if isinstance(overview, dict) else None
        ),
        "overview_unit_count": (
            overview.get("unit_count") if isinstance(overview, dict) else None
        ),
        "discovery_visited_source_files": selection["file_count"],
        "discovery_indexed_source_files": (
            overview.get("source_count") if isinstance(overview, dict) else None
        ),
        "discovery_skipped_source_files": (
            selection["file_count"] - int(overview.get("source_count", 0))
            if isinstance(overview, dict)
            else None
        ),
        "stderr_tail": cold.get("stderr") or warm.get("stderr") or "",
    }


def _problem_kind(problem: object) -> str:
    category = getattr(problem, "category", None)
    if category:
        return str(category)
    kind = getattr(problem, "kind", None)
    if kind:
        return str(kind)
    return type(problem).__name__


def _audit_file(task: tuple[str, str]) -> dict[str, Any]:
    workspace_raw, relative = task
    workspace = Path(workspace_raw)
    path = workspace / relative
    physical_lines = _line_count(path)
    try:
        from delphi_lsp.parser import DelphiParser
        from delphi_lsp.parser_backend import ParserBackend, ParserMode
        from delphi_lsp.source_reader import read_source_text

        text = read_source_text(path)
        include_paths = (str(path.parent), str(workspace / "FPCSource"))
        parser_mode = "delphiast_tolerant"
        parser = DelphiParser(
            include_paths=include_paths,
            backend=ParserBackend.DELPHIAST,
            mode=ParserMode.TOLERANT,
        )
        result = parser.parse(text, str(path), build_semantic=False)
        all_problems = [*result.preprocessed.problems, *result.problems]
        problems = [_problem_kind(problem) for problem in all_problems]
        descriptions = [
            str(getattr(problem, "message", type(problem).__name__))[:240]
            for problem in all_problems
        ]
        status = "parsed_with_problems" if problems else "parsed_ok"
        preprocessed_lines = result.preprocessed.text.count("\n") + (
            1
            if result.preprocessed.text
            and not result.preprocessed.text.endswith(("\n", "\r"))
            else 0
        )
        return {
            "path": relative,
            "physical_lines": physical_lines,
            "preprocessed_lines": preprocessed_lines,
            "status": status,
            "parser_mode": parser_mode,
            "fallback": "disabled_for_recovery_audit",
            "problems": problems,
            "problem_descriptions": descriptions,
        }
    except Exception as exc:  # noqa: BLE001 - each failure must remain countable.
        preprocessed_text = ""
        parser_mode = (
            parser_mode if "parser_mode" in locals() else "preprocessing_failed"
        )
        return {
            "path": relative,
            "physical_lines": physical_lines,
            "preprocessed_lines": preprocessed_text.count("\n")
            + (
                1
                if preprocessed_text
                and not preprocessed_text.endswith(("\n", "\r"))
                else 0
            ),
            "status": "failed",
            "parser_mode": parser_mode,
            "fallback": "disabled_for_recovery_audit",
            "problems": [f"exception:{type(exc).__name__}"],
            "problem_descriptions": [
                f"{type(exc).__name__}: {str(exc)[:220]}"
            ],
        }


def _run_parse_audit(
    workspace: Path,
    records: list[dict[str, Any]],
    workers: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tasks = [(str(workspace), str(record["path"])) for record in records]
    started = time.perf_counter()
    if workers == 1:
        audited = [_audit_file(task) for task in tasks]
        effective = 1
    else:
        effective = workers or min(8, max(1, (os.cpu_count() or 1) - 1))
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=effective,
            mp_context=context,
        ) as executor:
            audited = list(executor.map(_audit_file, tasks, chunksize=16))
    accounting = aggregate_parse_accounting(audited)
    fallback_usage = Counter(str(record["fallback"]) for record in audited)
    accounting.update(
        {
            "elapsed_seconds": time.perf_counter() - started,
            "workers": effective,
            "fallback_usage": dict(sorted(fallback_usage.items())),
            "scope": "all files in the prepared FPC manifest",
        }
    )
    return accounting, audited


def _slice_parse_accounting(
    audited: list[dict[str, Any]],
    selection: dict[str, Any],
) -> dict[str, Any]:
    selected_paths = {str(record["path"]) for record in selection["records"]}
    subset = [record for record in audited if record["path"] in selected_paths]
    accounting = aggregate_parse_accounting(subset)
    fallback_usage = Counter(str(record["fallback"]) for record in subset)
    accounting["fallback_usage"] = dict(sorted(fallback_usage.items()))
    return accounting


def _phase_breakdown(
    slice_root: Path,
    workers: int,
) -> dict[str, Any]:
    from delphi_lsp.agent_layers import build_codebase_index, layer_payload
    from delphi_lsp.source_reader import read_source_text

    source_paths = sorted(
        (
            path
            for path in slice_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".pas", ".inc"}
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    read_started = time.perf_counter()
    read_lines = 0
    for path in source_paths:
        text = read_source_text(path)
        read_lines += text.count("\n") + (
            1 if text and not text.endswith(("\n", "\r")) else 0
        )
    source_read_seconds = time.perf_counter() - read_started

    started = time.perf_counter()
    discovery_at: float | None = None
    last_outline: float | None = None
    relations_at: float | None = None

    def progress(event) -> None:
        nonlocal discovery_at, last_outline, relations_at
        now = time.perf_counter()
        if event.detail == "project discovery complete":
            discovery_at = now
        elif event.phase == "outline":
            last_outline = now
        elif event.phase == "relations":
            relations_at = now

    index = build_codebase_index(slice_root, workers=workers, on_progress=progress)
    indexed_at = time.perf_counter()
    payload_started = time.perf_counter()
    payload = layer_payload(index, "overview")
    payload_seconds = time.perf_counter() - payload_started
    discovery_end = discovery_at or indexed_at
    discovery_seconds = discovery_end - started
    outline_end = last_outline or indexed_at
    outline_seconds = max(0.0, outline_end - discovery_end)
    semantic_seconds = max(0.0, (relations_at or indexed_at) - outline_end)
    return {
        "measurement": "separate diagnostic run on the largest slice",
        "discovery_seconds": discovery_seconds,
        "source_reads_probe_seconds": source_read_seconds,
        "source_reads_probe_lines": read_lines,
        "preprocessing_parsing_and_model_seconds": outline_seconds,
        "semantic_index_assembly_seconds": semantic_seconds,
        "relation_indexing_seconds": 0.0,
        "relation_indexing_note": (
            "overview registers unit scopes; it does not traverse full references"
        ),
        "overview_payload_seconds": payload_seconds,
        "total_index_seconds": indexed_at - started,
        "source_count": payload.get("source_count"),
        "unit_count": payload.get("unit_count"),
        "limits": (
            "outline workers combine read, conditional preprocessing, outline parsing, "
            "and semantic-model construction; the read-only pass is a diagnostic probe"
        ),
    }


def _run_phase_process(
    workspace: Path,
    selection: dict[str, Any],
    workers: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="delphi-lsp-fpc-phase-") as raw_temp:
        slice_root = Path(raw_temp).resolve()
        _materialize_slice(workspace, selection, slice_root)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_internal-phases",
            str(slice_root),
            str(workers),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"timed_out": True, "timeout_seconds": timeout_seconds}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "error": "phase process returned invalid JSON",
            "stderr_tail": result.stderr[-2000:],
        }
    payload["returncode"] = result.returncode
    return payload


def _import_floor() -> dict[str, Any]:
    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "-c",
            "import delphi_lsp.agent_context",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    lines = result.stderr.splitlines()
    return {
        "wall_seconds": elapsed,
        "returncode": result.returncode,
        "importtime_line_count": len(lines),
        "importtime_sha256": hashlib.sha256(result.stderr.encode("utf-8")).hexdigest(),
        "slowest_tail": lines[-20:],
    }


def _total_ram_bytes() -> int | None:
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
        return None
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None


def _platform_info() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "total_ram_bytes": _total_ram_bytes(),
    }


def _projection(slices: list[dict[str, Any]]) -> dict[str, Any]:
    largest = max(slices, key=lambda item: item["line_count"], default=None)
    system = platform.system()
    processor = platform.processor().casefold()
    target_host = system == "Windows" and "i5" in processor
    factor = 1.0 if target_host else 4.0
    cold = _number(
        largest.get("cold_wall_seconds") if largest else None,
        math.nan,
    )
    warm = _number(
        largest.get("warm_wall_seconds") if largest else None,
        math.nan,
    )
    return {
        "target": "standard Windows Core i5 PC",
        "factor": factor,
        "assumption": (
            "native target measurement"
            if target_host
            else "conservative 4x time de-rating for a faster non-target host"
        ),
        "projected_largest_cold_seconds": (
            cold * factor if math.isfinite(cold) else None
        ),
        "projected_largest_warm_seconds": (
            warm * factor if math.isfinite(warm) else None
        ),
        "projection_is_release_gate": False,
    }


def _worker_sweep(
    workspace: Path,
    records: list[dict[str, Any]],
    timeout_seconds: float,
    worker_values: tuple[int, ...],
) -> dict[str, Any]:
    selection = select_slice(records, 1_000_000)
    results = [
        _benchmark_slice(
            workspace,
            selection,
            workers,
            timeout_seconds=timeout_seconds,
            skip_warm=True,
        )
        for workers in worker_values
    ]
    baseline = next(
        (item for item in results if item["workers"] == 1),
        results[0],
    )
    baseline_seconds = baseline["cold_wall_seconds"]
    return {
        "target_lines": selection["target_lines"],
        "line_count": selection["line_count"],
        "results": [
            {
                "workers": item["workers"],
                "cold_wall_seconds": item["cold_wall_seconds"],
                "peak_rss_bytes": item["peak_rss_bytes"],
                "speedup_vs_one_worker": (
                    baseline_seconds / item["cold_wall_seconds"]
                    if item["cold_wall_seconds"] > 0
                    else None
                ),
            }
            for item in results
        ],
    }


def run_benchmark(
    workspace: Path,
    *,
    lock_path: Path = LOCK_DEFAULT,
    targets: list[int] | tuple[int, ...] = DEFAULT_TARGETS,
    workers: int = 0,
    worker_values: tuple[int, ...] = (0, 1, 2, 4),
    parse_workers: int = 0,
    timeout_seconds: float = 300.0,
    parse_audit: bool = True,
    run_worker_sweep: bool = True,
    run_phase_breakdown: bool = True,
    skip_warm: bool = False,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    lock_path = lock_path.expanduser().resolve()
    manifest_path, manifest, records = _load_verified_manifest(workspace, lock_path)
    selections = [select_slice(records, target) for target in targets]
    slices: list[dict[str, Any]] = []
    for ordinal, selection in enumerate(selections, start=1):
        print(
            f"[fpc] slice {ordinal}/{len(selections)}: "
            f"{selection['line_count']} lines",
            file=sys.stderr,
            flush=True,
        )
        slices.append(
            _benchmark_slice(
                workspace,
                selection,
                workers,
                timeout_seconds=timeout_seconds,
                skip_warm=skip_warm,
            )
        )
    scaling = fit_scaling(slices)
    if parse_audit:
        print("[fpc] parser compatibility audit", file=sys.stderr, flush=True)
        parse_accounting, audited = _run_parse_audit(
            workspace,
            records,
            parse_workers,
        )
        for item, selection in zip(slices, selections):
            item["parse_accounting"] = _slice_parse_accounting(
                audited,
                selection,
            )
    else:
        parse_accounting = {
            "skipped": True,
            "accepted_ratio": None,
            "reason": "disabled by caller",
        }
    if run_phase_breakdown:
        print("[fpc] phase diagnostic", file=sys.stderr, flush=True)
        phase_breakdown = _run_phase_process(
            workspace,
            selections[-1],
            workers,
            timeout_seconds,
        )
    else:
        phase_breakdown = {"skipped": True}
    if run_worker_sweep:
        print("[fpc] worker sweep", file=sys.stderr, flush=True)
        worker_sweep = _worker_sweep(
            workspace,
            records,
            timeout_seconds,
            worker_values,
        )
    else:
        worker_sweep = {"skipped": True}
    manifest_summary = {
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "repository": manifest["corpora"][0]["repository"],
        "revision": manifest["corpora"][0]["revision"],
        "target_lines": manifest["target_lines"],
        "line_count": manifest["line_count"],
        "file_count": manifest["file_count"],
        "anchors": manifest["corpora"][0].get("anchors", []),
        "source_scope": manifest.get("source_scope"),
    }
    observations = [
        (
            "The overview command uses the outline semantic path for every source; "
            "the recovery-oriented DelphiAST parser audit is reported separately "
            "(delphi_lsp/agent_layers.py:38-127, delphi_lsp/parser.py:88-108)."
        ),
        (
            "The compatibility audit explicitly selects DelphiAST tolerant mode so "
            "every recoverable FPC problem remains countable. Strict mode invokes "
            "the legacy Lark parser for preprocessed inputs up to 32 KiB and is not "
            "the overview path (delphi_lsp/parser.py:99-108)."
        ),
        (
            "The view command did not create a persistent .delphi-lsp cache in "
            "the temporary slices; warm measurements therefore include only OS "
            "filesystem caching and a fresh Python process "
            "(delphi_lsp/agent_cli.py:313-321)."
        ),
        (
            "Manifest verification covers every selected file before timing; "
            "slice visited/indexed/skipped source counts expose discovery omissions "
            "(delphi_lsp/project_discovery.py:353-402)."
        ),
        (
            "FPC .pas and .inc files are included. Other suffixes such as .pp are "
            "excluded because the product workspace scanner does not index them "
            "(delphi_lsp/agent_layers.py:76-81)."
        ),
        (
            "The older mixed-corpus builder has POSIX-only /tmp defaults; these "
            "FPC scripts use tempfile.gettempdir() or an explicit workspace "
            "(scripts/build_github_performance_corpus.py:17-18)."
        ),
        (
            "Spawned outline workers each import the Python module tree; peak RSS "
            "in this report covers the CLI parent and does not aggregate worker "
            "process RSS (delphi_lsp/parallel_outline.py:123-181)."
        ),
    ]
    if not parse_accounting.get("skipped"):
        observations.append(
            "The FPC recovery audit returned "
            f"{parse_accounting['files_with_problems']}/"
            f"{parse_accounting['files_total']} files with reported problems, "
            f"including {parse_accounting['problem_types'].get('syntax', 0)} "
            "syntax recoveries (delphi_lsp/parser.py:88-127)."
        )
    effective_budgets = dict(DEFAULT_BUDGETS if budgets is None else budgets)
    platform_info = _platform_info()
    projection = _projection(slices)
    return assemble_report(
        workspace=workspace,
        manifest=manifest_summary,
        slices=slices,
        parse_accounting=parse_accounting,
        scaling=scaling,
        phase_breakdown=phase_breakdown,
        worker_sweep=worker_sweep,
        import_floor=_import_floor(),
        platform_info=platform_info,
        budgets=effective_budgets,
        projection=projection,
        observations=observations,
    )


def _internal_overview(argv: list[str]) -> int:
    from delphi_lsp.agent_cli import main as agent_main

    root, raw_workers = argv
    capture = io.StringIO()
    started = time.process_time()
    with redirect_stdout(capture):
        returncode = agent_main(
            [
                "view",
                "--root",
                root,
                "--layer",
                "overview",
                "--format",
                "json",
                "--workers",
                "auto" if raw_workers == "0" else raw_workers,
            ]
        )
    cpu_seconds = time.process_time() - started
    try:
        overview = json.loads(capture.getvalue())
    except json.JSONDecodeError:
        overview = None
    print(
        json.dumps(
            {
                "returncode": returncode,
                "cpu_seconds": cpu_seconds,
                "peak_rss_bytes": _peak_rss_bytes(),
                "overview": overview,
            },
            sort_keys=True,
        )
    )
    return returncode


def _parse_integer_list(value: str) -> list[int]:
    try:
        targets = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be comma-separated integers") from exc
    if not targets or any(target < 0 for target in targets):
        raise argparse.ArgumentTypeError("values must be zero or positive")
    return targets


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "_internal-overview":
        return _internal_overview(raw_argv[1:])
    if raw_argv and raw_argv[0] == "_internal-phases":
        print(json.dumps(_phase_breakdown(Path(raw_argv[1]), int(raw_argv[2]))))
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=LOCK_DEFAULT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--sizes",
        type=_parse_integer_list,
        default=list(DEFAULT_TARGETS),
        help="comma-separated line targets",
    )
    parser.add_argument(
        "--workers",
        type=_parse_integer_list,
        default=[0, 1, 2, 4],
        help="comma-separated worker sweep; first value is used for the scaling curve",
    )
    parser.add_argument("--parse-workers", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--budget-cold", type=float, default=60.0)
    parser.add_argument("--budget-warm", type=float, default=5.0)
    parser.add_argument("--budget-rss-gib", type=float, default=11.0)
    parser.add_argument("--min-parse-ratio", type=float, default=0.95)
    parser.add_argument("--max-scaling-exponent", type=float, default=1.15)
    parser.add_argument("--no-parse-audit", action="store_true")
    parser.add_argument("--no-worker-sweep", action="store_true")
    parser.add_argument("--no-phase-breakdown", action="store_true")
    parser.add_argument("--skip-warm", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(raw_argv)
    if args.parse_workers < 0:
        parser.error("worker counts must be zero or positive")
    if any(size <= 0 for size in args.sizes):
        parser.error("sizes must be positive")
    if any(worker > 32 for worker in args.workers):
        parser.error("workers must be between 0 and 32")
    if (
        args.budget_cold <= 0
        or args.budget_warm <= 0
        or args.budget_rss_gib <= 0
        or not 0 <= args.min_parse_ratio <= 1
        or args.max_scaling_exponent <= 0
    ):
        parser.error("budget values must be positive and parse ratio must be 0..1")
    budgets = {
        "max_3m_cold_seconds": args.budget_cold,
        "max_3m_warm_seconds": args.budget_warm,
        "max_peak_rss_bytes": int(args.budget_rss_gib * 1024**3),
        "min_parse_accepted_ratio": args.min_parse_ratio,
        "max_scaling_exponent": args.max_scaling_exponent,
    }
    try:
        report = run_benchmark(
            args.workspace,
            lock_path=args.lock,
            targets=args.sizes,
            workers=args.workers[0],
            worker_values=tuple(args.workers),
            parse_workers=args.parse_workers,
            timeout_seconds=args.timeout_seconds,
            parse_audit=not args.no_parse_audit,
            run_worker_sweep=not args.no_worker_sweep,
            run_phase_breakdown=not args.no_phase_breakdown,
            skip_warm=args.skip_warm,
            budgets=budgets,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"benchmark failed: {exc}\n")
    output = args.output.expanduser().resolve()
    if output.is_relative_to(ROOT):
        parser.exit(2, "benchmark failed: report output must be outside the repository\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    print(f"report: {output}", file=sys.stderr)
    return 0 if args.report_only or report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
