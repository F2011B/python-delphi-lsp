#!/usr/bin/env python3
"""Benchmark Protocol v2 actions against a verified external GitHub corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_github_performance_corpus import (  # noqa: E402
    LOCK_DEFAULT,
    MANIFEST_NAME,
    load_corpus_lock,
    verify_manifest,
)
from delphi_lsp.agent_context import AgentContext  # noqa: E402


MINIMUM_LINES = 2_000_000
COLD_SECONDS = 60.0
WARM_SECONDS = 1.0


def _measure(operation: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = operation()
    return result, time.perf_counter() - started


def _posix_peak_rss_bytes() -> int:
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _windows_peak_rss_bytes() -> int:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


def _peak_rss_bytes() -> int:
    if sys.platform == "win32":
        return _windows_peak_rss_bytes()
    return _posix_peak_rss_bytes()


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float("inf")


def release_failures(report: dict[str, Any]) -> list[str]:
    manifest = report.get("manifest", {})
    timings = report.get("timings_seconds", {})
    total_lines = manifest.get("line_count", 0) if isinstance(manifest, dict) else 0
    open_seconds = _number(timings.get("open")) if isinstance(timings, dict) else float("inf")
    cold_find = _number(timings.get("cold_find")) if isinstance(timings, dict) else float("inf")
    warm_find = _number(timings.get("warm_find")) if isinstance(timings, dict) else float("inf")
    failures: list[str] = []
    if not isinstance(total_lines, int) or isinstance(total_lines, bool) or total_lines < MINIMUM_LINES:
        failures.append(f"corpus has {total_lines} lines; need at least {MINIMUM_LINES}")
    if open_seconds + cold_find >= COLD_SECONDS:
        failures.append(
            f"cold open plus find is {open_seconds + cold_find:.3f}s; must be below {COLD_SECONDS:.3f}s"
        )
    if warm_find >= WARM_SECONDS:
        failures.append(f"warm find is {warm_find:.3f}s; must be below {WARM_SECONDS:.3f}s")
    return failures


def _load_verified_manifest(workspace: Path, lock_path: Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = workspace / MANIFEST_NAME
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read corpus manifest {manifest_path}: {exc}") from exc
    verify_manifest(workspace, manifest)
    lock = load_corpus_lock(lock_path)
    locked = {
        str(corpus["name"]): (str(corpus["repository"]), str(corpus["revision"]))
        for corpus in lock["corpora"]
    }
    recorded = {
        str(corpus.get("name")): (str(corpus.get("repository")), str(corpus.get("revision")))
        for corpus in manifest.get("corpora", [])
        if isinstance(corpus, dict)
    }
    if recorded != locked:
        raise RuntimeError("Manifest mismatch: corpus repositories or revisions differ from the lock.")
    if manifest.get("target_lines") != lock.get("target_lines"):
        raise RuntimeError("Manifest mismatch: target_lines differs from the lock.")
    return manifest_path, manifest


def run_benchmark(
    workspace: Path,
    query: str,
    *,
    lock_path: Path = LOCK_DEFAULT,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    manifest_path, manifest = _load_verified_manifest(workspace, lock_path)

    context, open_seconds = _measure(lambda: AgentContext.open(workspace))
    open_response, open_action_seconds = _measure(lambda: context.handle({"action": "open", "max_items": 20}))
    cold_response, cold_seconds = _measure(
        lambda: context.handle({"action": "find", "query": query, "max_items": 50})
    )
    warm_response, warm_seconds = _measure(
        lambda: context.handle({"action": "find", "query": query, "max_items": 50})
    )
    cold_items = cold_response.result if isinstance(cold_response.result, list) else []
    target = next(
        (item for item in cold_items if str(item.get("name", "")).casefold() == query.casefold()),
        cold_items[0] if cold_items else None,
    )
    if not isinstance(target, dict) or not target.get("target_id"):
        raise RuntimeError(f"Query {query!r} returned no target.")
    target_id = str(target["target_id"])
    focus_response, focus_seconds = _measure(
        lambda: context.handle({"action": "focus", "target_id": target_id, "max_items": 10})
    )
    inspect_response, inspect_seconds = _measure(
        lambda: context.handle(
            {
                "action": "inspect",
                "target_id": target_id,
                "detail": "declaration",
                "max_items": 10,
                "max_chars": 12_000,
            }
        )
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "workspace": str(workspace),
        "manifest": {
            "path": str(manifest_path),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "target_lines": manifest["target_lines"],
            "line_count": manifest["line_count"],
            "file_count": manifest["file_count"],
            "corpora": [
                {key: corpus[key] for key in ("name", "repository", "revision", "line_count", "file_count")}
                for corpus in manifest["corpora"]
            ],
        },
        "query": query,
        "result_count": cold_response.page.total,
        "warm_result_count": warm_response.page.total,
        "open_item_count": open_response.page.total,
        "focus_item_count": focus_response.page.total,
        "inspect_item_count": inspect_response.page.total,
        "target": {
            "target_id": target_id,
            "name": target.get("name"),
            "qualified_name": target.get("qualified_name"),
            "path": target.get("path"),
            "line": target.get("line"),
        },
        "timings_seconds": {
            "open": open_seconds,
            "open_action": open_action_seconds,
            "cold_find": cold_seconds,
            "warm_find": warm_seconds,
            "focus": focus_seconds,
            "inspect": inspect_seconds,
        },
        "peak_rss_bytes": _peak_rss_bytes(),
        "budgets": {
            "minimum_lines": MINIMUM_LINES,
            "cold_open_plus_find_seconds": COLD_SECONDS,
            "warm_find_seconds": WARM_SECONDS,
        },
        "failures": [],
    }
    failures = release_failures(report)
    report["failures"] = failures
    report["status"] = "fail" if failures else "pass"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=LOCK_DEFAULT)
    parser.add_argument("--query", default="TSynLog")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args.workspace, args.query, lock_path=args.lock)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if args.report_only or report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
