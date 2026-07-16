from __future__ import annotations

import json
from pathlib import Path

import pytest

from delphi_lsp.agent_context import AgentContext
from delphi_lsp.agent_protocol import AgentProtocolError


def write_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.strip() + "\n", encoding="utf-8")


def open_metric_context(tmp_path: Path) -> AgentContext:
    write_source(
        tmp_path / "Main.dpr",
        """
program Main;
uses Alpha in 'Alpha.pas';
begin
end.
""",
    )
    write_source(
        tmp_path / "Alpha.pas",
        """
unit Alpha;
interface
uses Beta;
procedure Score(Value: Integer);
implementation
procedure Score(Value: Integer);
begin
  if Value > 0 then
    while Value > 1 do
      Value := Value - 1;
end;
end.
""",
    )
    write_source(
        tmp_path / "Beta.pas",
        """
unit Beta;
interface
type IService = interface end;
implementation
end.
""",
    )
    return AgentContext.open(tmp_path, "Main.dpr")


def response_items(response) -> list[dict[str, object]]:
    assert isinstance(response.result, list)
    return response.result


def test_metrics_returns_project_summary_and_unit_cards(tmp_path: Path) -> None:
    context = open_metric_context(tmp_path)

    response = context.handle(
        {"action": "metrics", "max_items": 50, "max_chars": 40000}
    )
    items = response_items(response)

    assert items[0]["item_type"] == "project_metrics"
    assert items[0]["total_loc"] == 21
    assert items[0]["unit_count"] == 3
    unit_items = [item for item in items if item["item_type"] == "unit_metrics"]
    assert [item["name"] for item in unit_items] == ["Alpha", "Beta", "Main"]
    assert json.dumps(response.to_mapping(), allow_nan=False)


def test_metrics_filters_units_and_selects_open_unit_id(tmp_path: Path) -> None:
    context = open_metric_context(tmp_path)
    opened = context.handle({"action": "open", "max_items": 50, "max_chars": 40000})
    alpha_open = next(
        item for item in response_items(opened)
        if item.get("item_type") == "unit" and item.get("name") == "Alpha"
    )

    filtered = context.handle(
        {"action": "metrics", "query": "ALPHA", "max_items": 50, "max_chars": 40000}
    )
    detailed = context.handle(
        {
            "action": "metrics",
            "target_id": alpha_open["unit_id"],
            "detail": "members",
            "max_items": 50,
            "max_chars": 40000,
        }
    )

    assert [item["name"] for item in response_items(filtered)] == ["Alpha"]
    detail = response_items(detailed)[0]
    assert detail["unit_id"] == alpha_open["unit_id"]
    assert detail["cyclomatic"]["maximum"] == 3
    assert detail["cyclomatic"]["routines"] == [
        {"line": 6, "name": "Score", "value": 3}
    ]
    assert detail["internal_dependencies"] == ["Beta"]
    assert detail["halstead"]["volume"] > 0
    assert detail["symbol_counts"]["procedure"] >= 1


def test_metrics_paginates_with_existing_cursor_contract(tmp_path: Path) -> None:
    context = open_metric_context(tmp_path)
    request = {"action": "metrics", "max_items": 1, "max_chars": 40000}

    first = context.handle(request)
    second = context.handle({**request, "cursor": first.page.next_cursor})

    assert response_items(first)[0]["item_type"] == "project_metrics"
    assert first.page.truncated is True
    assert response_items(second)[0]["item_type"] == "unit_metrics"


def test_metrics_rejects_source_details_and_unknown_unit_targets(tmp_path: Path) -> None:
    context = open_metric_context(tmp_path)

    with pytest.raises(AgentProtocolError) as invalid_detail:
        context.handle({"action": "metrics", "detail": "body"})
    assert invalid_detail.value.code == "invalid_detail"

    with pytest.raises(AgentProtocolError) as missing_target:
        context.handle({"action": "metrics", "target_id": "target_v2_missing"})
    assert missing_target.value.code == "target_not_found"


def test_metrics_cache_invalidates_when_project_source_changes(tmp_path: Path) -> None:
    context = open_metric_context(tmp_path)
    first = context.handle(
        {"action": "metrics", "max_items": 50, "max_chars": 40000}
    )
    first_total = response_items(first)[0]["total_loc"]

    alpha = tmp_path / "Alpha.pas"
    alpha.write_text(alpha.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    second = context.handle(
        {"action": "metrics", "max_items": 50, "max_chars": 40000}
    )

    assert second.workspace_revision != first.workspace_revision
    assert response_items(second)[0]["total_loc"] == first_total + 1
