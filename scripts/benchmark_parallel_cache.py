#!/usr/bin/env python3
"""Compare serial and parallel cold outline-index builds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delphi_lsp.agent_layers import build_codebase_index, layer_payload  # noqa: E402
from delphi_lsp.parallel_outline import parse_worker_setting  # noqa: E402


def _measure(operation: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = operation()
    return result, time.perf_counter() - started


def benchmark(root: Path, workers: int) -> dict[str, object]:
    canonical = root.resolve()
    sequential, sequential_seconds = _measure(
        lambda: build_codebase_index(canonical, workers=1)
    )
    parallel, parallel_seconds = _measure(
        lambda: build_codebase_index(canonical, workers=workers)
    )
    sequential_cards = layer_payload(sequential, "unit")["items"]
    parallel_cards = layer_payload(parallel, "unit")["items"]
    return {
        "sequential_seconds": sequential_seconds,
        "parallel_seconds": parallel_seconds,
        "speedup": sequential_seconds / parallel_seconds if parallel_seconds else float("inf"),
        "workers_effective": parallel.parallel_stats.effective_workers,
        "targets_equal": parallel_cards == sequential_cards,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare serial and bounded-parallel Delphi outline indexing."
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workers", type=parse_worker_setting, default=0, metavar="auto|N")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = benchmark(args.root, args.workers)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"sequential: {report['sequential_seconds']:.6f}s")
        print(f"parallel:   {report['parallel_seconds']:.6f}s")
        print(f"speedup:    {report['speedup']:.3f}x")
        print(f"workers:    {report['workers_effective']}")
        print(f"equal:      {str(report['targets_equal']).lower()}")
    return 0 if report["targets_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
