from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressEvent:
    """A package-controlled snapshot of Delphi indexing progress."""

    phase: str
    language: str
    path: str
    files_discovered: int
    files_completed: int
    files_total: int | None
    lines_processed: int
    symbols_discovered: int
    cached_files: int
    detail: str


ProgressCallback = Callable[[ProgressEvent], None]


__all__ = ["ProgressCallback", "ProgressEvent"]
