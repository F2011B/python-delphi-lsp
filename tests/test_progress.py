from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from delphi_lsp.agent_layers import build_codebase_index
from delphi_lsp.project_indexer import ProjectIndexer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_codebase_index_reports_frozen_per_file_progress_and_completion(tmp_path: Path) -> None:
    _write(tmp_path / "One.pas", "unit One; interface implementation end.\n")
    _write(tmp_path / "Two.pas", "unit Two; interface implementation end.\n")
    events: list[object] = []

    build_codebase_index(tmp_path, on_progress=events.append)

    assert events[0].phase == "discovery"
    assert events[-1].phase == "complete"
    outlines = [event for event in events if event.phase == "outline"]
    assert [Path(event.path).name for event in outlines] == ["One.pas", "Two.pas"]
    assert [event.files_completed for event in outlines] == [1, 2]
    assert outlines[-1].lines_processed == 2
    assert outlines[-1].symbols_discovered >= 2
    discovery = [event for event in events if event.phase == "discovery" and event.path.endswith(".pas")]
    assert [event.files_discovered for event in discovery] == [1, 2]
    assert all(event.files_total is None for event in discovery)
    for field in ("files_discovered", "files_completed", "files_total", "lines_processed", "symbols_discovered", "cached_files"):
        values = [getattr(event, field) for event in events if getattr(event, field) is not None]
        assert values == sorted(values)
    assert all(event.language == "delphi" for event in events)
    with pytest.raises(FrozenInstanceError):
        events[0].phase = "changed"  # type: ignore[misc]


def test_project_indexer_reports_each_parsed_unit_and_propagates_callback_errors(tmp_path: Path) -> None:
    _write(tmp_path / "Main.dpr", "program Main; uses Worker in 'Worker.pas'; begin end.\n")
    _write(tmp_path / "Worker.pas", "unit Worker; interface implementation end.\n")
    events: list[object] = []

    ProjectIndexer(on_progress=events.append).index(str(tmp_path / "Main.dpr"))

    parsed = [event for event in events if event.phase == "detail"]
    assert [Path(event.path).name for event in parsed] == ["Main.dpr", "Worker.pas"]
    assert [event.files_completed for event in parsed] == [1, 2]

    with pytest.raises(RuntimeError, match="callback failed"):
        ProjectIndexer(on_progress=lambda _event: (_ for _ in ()).throw(RuntimeError("callback failed"))).index(
            str(tmp_path / "Main.dpr")
        )
