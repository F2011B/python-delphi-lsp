#!/usr/bin/env python3
"""Benchmark lazy Protocol v3 CPG queries on a four-million-line workspace."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delphi_lsp.agent_context import AgentContext  # noqa: E402


MINIMUM_LINES = 4_000_000
MINIMUM_LEGACY_RATIO = 0.95
GENERATED_UNIT_LINES = 1_000
PASCAL_EXTENSIONS = frozenset({".pas", ".pp", ".inc", ".dpr", ".dpk"})
ROUTINE_KINDS = frozenset(
    {"method", "function", "procedure", "constructor", "destructor"}
)


def _measure(operation: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = operation()
    return result, time.perf_counter() - started


def _peak_rss_bytes() -> int:
    if sys.platform == "win32":
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
        process = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess()
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        if not psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)

    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float("inf")


def release_failures(report: dict[str, Any]) -> list[str]:
    loc = report.get("loc")
    control = _number(report.get("legacy_control_queries_per_second"))
    current = _number(report.get("legacy_queries_per_second"))
    parsed = report.get("sources_parsed_for_cpg")
    first = _number(report.get("cpg_first_seconds"))
    cached = _number(report.get("cpg_cached_seconds"))
    failures: list[str] = []
    if not isinstance(loc, int) or isinstance(loc, bool) or loc < MINIMUM_LINES:
        failures.append(f"corpus has {loc} lines; need at least {MINIMUM_LINES}")
    if current < control * MINIMUM_LEGACY_RATIO:
        failures.append("legacy throughput regressed by more than 5 percent")
    if parsed != 1:
        failures.append("CPG must parse exactly one source")
    if cached > first:
        failures.append("cached CPG query is slower than the first CPG query")
    return failures


def generate_workspace(root: Path, *, minimum_lines: int = MINIMUM_LINES) -> int:
    """Generate deterministic, parser-friendly Delphi sources for release testing."""
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise RuntimeError(f"generated workspace must be empty: {root}")
    unit_count = (minimum_lines + GENERATED_UNIT_LINES - 1) // GENERATED_UNIT_LINES
    filler_lines = GENERATED_UNIT_LINES - 13
    for index in range(unit_count):
        name = f"Generated{index:05d}"
        routine = "BenchmarkTarget" if index == 0 else f"Routine{index:05d}"
        source = [
            f"unit {name};",
            "interface",
            f"procedure {routine};",
            "implementation",
            f"procedure {routine};",
            "var",
            "  Value: Integer;",
            "begin",
            "  Value := 1;",
            "  if Value > 0 then",
            "    Value := Value + 1;",
            "end;",
            *("// deterministic benchmark filler" for _ in range(filler_lines)),
            "end.",
        ]
        (root / f"{name}.pas").write_text(
            "\n".join(source) + "\n",
            encoding="utf-8",
        )
    return unit_count * GENERATED_UNIT_LINES


def workspace_loc(root: Path) -> int:
    manifest_path = root / "corpus-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid corpus manifest: {error}") from error
        line_count = manifest.get("line_count")
        files = manifest.get("file_count")
        if (
            not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or not isinstance(files, int)
            or isinstance(files, bool)
            or line_count < 1
            or files < 1
        ):
            raise RuntimeError("invalid corpus manifest counts")
        return line_count

    total = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in PASCAL_EXTENSIONS:
            continue
        with path.open("rb") as source:
            total += sum(chunk.count(b"\n") for chunk in iter(lambda: source.read(1024**2), b""))
    return total


def _result_items(response: Any) -> list[dict[str, object]]:
    return response.result if isinstance(response.result, list) else []


def _find_target(
    context: AgentContext,
    query: str,
) -> tuple[str, list[dict[str, object]]]:
    response = context.handle(
        {
            "action": "find",
            "query": query,
            "max_items": 50,
            "max_chars": 40_000,
        }
    )
    items = _result_items(response)
    target = next(
        (
            item
            for item in items
            if item.get("kind") in ROUTINE_KINDS and item.get("target_id")
        ),
        None,
    )
    if target is None:
        raise RuntimeError(
            f"query {query!r} returned no routine target; choose a narrower query"
        )
    return str(target["target_id"]), items


def _run_requests(
    context: AgentContext,
    requests: Sequence[dict[str, object]],
    *,
    minimum_seconds: float,
    request_count: int | None = None,
) -> tuple[int, float]:
    started = time.perf_counter()
    completed = 0
    while request_count is None or completed < request_count:
        for request in requests:
            if request_count is not None and completed >= request_count:
                break
            context.handle(request)
            completed += 1
        if request_count is None and time.perf_counter() - started >= minimum_seconds:
            break
    elapsed = time.perf_counter() - started
    return completed, elapsed


def run_benchmark(
    root: Path,
    *,
    query: str = "BenchmarkTarget",
    workers: int = 0,
    throughput_seconds: float = 1.0,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    loc = workspace_loc(root)
    started = time.perf_counter()
    context = AgentContext.open(
        root,
        workers=workers,
        revision_check_interval_seconds=30.0,
    )
    context.prewarm_navigation()
    prewarm_seconds = time.perf_counter() - started
    if context.cpg_sources_parsed != 0:
        raise RuntimeError("navigation prewarm parsed a CPG source")

    target_id, _ = _find_target(context, query)
    requests = (
        {"action": "open", "max_items": 5, "max_chars": 4_000},
        {
            "action": "find",
            "query": query,
            "max_items": 10,
            "max_chars": 8_000,
        },
        {
            "action": "inspect",
            "target_id": target_id,
            "detail": "summary",
            "max_items": 10,
            "max_chars": 8_000,
        },
    )
    _run_requests(context, requests, minimum_seconds=0.0, request_count=len(requests))
    control_count, control_seconds = _run_requests(
        context,
        requests,
        minimum_seconds=max(0.1, throughput_seconds),
    )
    control_qps = control_count / max(control_seconds, 1e-9)

    cpg_request = {
        "action": "cpg",
        "target_id": target_id,
        "graph": "full",
        "direction": "out",
        "depth": 4,
        "max_items": 50,
        "max_chars": 40_000,
    }
    first_response, cpg_first_seconds = _measure(
        lambda: context.handle(cpg_request)
    )
    cached_response, cpg_cached_seconds = _measure(
        lambda: context.handle(cpg_request)
    )
    after_count, after_seconds = _run_requests(
        context,
        requests,
        minimum_seconds=0.0,
        request_count=control_count,
    )
    legacy_qps = after_count / max(after_seconds, 1e-9)
    if first_response.result != cached_response.result:
        raise RuntimeError("cached CPG response changed")

    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "workspace": str(root),
        "loc": loc,
        "prewarm_seconds": prewarm_seconds,
        "legacy_control_queries_per_second": control_qps,
        "legacy_queries_per_second": legacy_qps,
        "legacy_throughput_ratio": legacy_qps / max(control_qps, 1e-9),
        "cpg_first_seconds": cpg_first_seconds,
        "cpg_cached_seconds": cpg_cached_seconds,
        "cpg_cache_bytes": context.cpg_cache_bytes,
        "sources_parsed_for_cpg": context.cpg_sources_parsed,
        "peak_rss_bytes": _peak_rss_bytes(),
        "target_id": target_id,
        "failures": [],
    }
    failures = release_failures(report)
    report["failures"] = failures
    report["status"] = "fail" if failures else "pass"
    return report


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--root", type=Path)
    sources.add_argument("--generate", action="store_true")
    parser.add_argument("--generated-root", type=Path)
    parser.add_argument("--generated-lines", type=int, default=MINIMUM_LINES)
    parser.add_argument("--query", default="BenchmarkTarget")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--throughput-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.generate:
            if args.generated_root is not None:
                generate_workspace(
                    args.generated_root,
                    minimum_lines=args.generated_lines,
                )
                report = run_benchmark(
                    args.generated_root,
                    query=args.query,
                    workers=args.workers,
                    throughput_seconds=args.throughput_seconds,
                )
            else:
                with tempfile.TemporaryDirectory(prefix="delphi-cpg-4m-") as directory:
                    generated_root = Path(directory)
                    generate_workspace(
                        generated_root,
                        minimum_lines=args.generated_lines,
                    )
                    report = run_benchmark(
                        generated_root,
                        query=args.query,
                        workers=args.workers,
                        throughput_seconds=args.throughput_seconds,
                    )
        else:
            if args.root is None:
                raise RuntimeError("--root is required")
            report = run_benchmark(
                args.root,
                query=args.query,
                workers=args.workers,
                throughput_seconds=args.throughput_seconds,
            )
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    _write_report(report, args.output)
    return 0 if args.report_only or report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
