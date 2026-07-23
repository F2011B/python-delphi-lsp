from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import pytest
import textwrap

from delphi_lsp.agent_cache import (
    BudgetResult,
    CacheBudget,
    CacheStats,
    cache_warning,
    estimate_deep_size,
    parse_memory_size,
)
from delphi_lsp.agent_context import AgentContext

def write_source(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")


def test_estimate_deep_size_handles_cycles_dataclasses_and_slots() -> None:
    @dataclass
    class Payload:
        values: list[object]

    class SlotPayload:
        __slots__ = ("payload", "__weakref__")

        def __init__(self, payload: object) -> None:
            self.payload = payload

    cyclic: list[object] = []
    cyclic.append(cyclic)
    value = SlotPayload(Payload([cyclic, {"payload": "value"}]))

    assert estimate_deep_size(value) > 0


def test_estimate_deep_size_counts_opaque_objects_without_introspection() -> None:
    class Opaque:
        def __getattribute__(self, name: str) -> object:
            if name == "__dict__":
                raise RuntimeError("opaque object")
            return super().__getattribute__(name)

    assert estimate_deep_size(Opaque()) > 0


def test_estimate_deep_size_ignores_broken_size_and_mapping_enumeration() -> None:
    class BrokenSize:
        def __sizeof__(self) -> int:
            raise RuntimeError("size unavailable")

    class BrokenMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def items(self):
            raise RuntimeError("items unavailable")

    assert estimate_deep_size(BrokenSize()) >= 0
    assert estimate_deep_size(BrokenMapping()) >= 0


def test_estimate_deep_size_reads_slotted_dataclass_fields_once() -> None:
    reads = 0

    @dataclass(slots=True)
    class SlottedPayload:
        value: object

        def __getattribute__(self, name: str) -> object:
            nonlocal reads
            if name == "value":
                reads += 1
            return super().__getattribute__(name)

    assert estimate_deep_size(SlottedPayload("payload")) > 0
    assert reads == 1


def test_navigation_cache_eviction_preserves_selection_and_rebuilds(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'UnitA.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "UnitA.pas",
        """
        unit UnitA;
        interface
        type
          TCustomer = class
          end;
        implementation
        end.
        """,
    )

    context = AgentContext.open(tmp_path)
    result = context.handle({"action": "find", "query": "TCustomer"})

    assert [item["name"] for item in result.result] == ["TCustomer"]
    assert context.navigation_cache_is_warm
    assert estimate_deep_size(context.cache_roots()) > 0
    active_project_id = context.workspace.active_project_id

    context.evict_auxiliary_caches()

    assert context.navigation_cache_is_warm

    context.evict_navigation_caches()

    assert not context.navigation_cache_is_warm
    assert context.workspace.active_project_id == active_project_id

    rebuilt = context.handle({"action": "find", "query": "TCustomer"})

    assert [item["name"] for item in rebuilt.result] == ["TCustomer"]
    assert context.navigation_cache_is_warm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512M", 512 * 1024**2),
        ("1G", 1024**3),
        ("4096K", 4096 * 1024),
        ("1048576", 1048576),
    ],
)
def test_parse_memory_size(text: str, expected: int) -> None:
    assert parse_memory_size(text) == expected


@pytest.mark.parametrize(
    "text",
    ["0", "-1", "0K", "-2G", "1024T", "12.5M", ""],
)
def test_parse_memory_size_rejects_invalid_values(text: str) -> None:
    with pytest.raises(ValueError, match="Memory size must be a positive integer with optional K, M, or G suffix."):
        parse_memory_size(text)


def test_cache_stats_defaults() -> None:
    stats = CacheStats()

    assert stats.requests == 0
    assert stats.warm_hits == 0
    assert stats.rebuilds == 0
    assert stats.invalidations == 0
    assert stats.evictions == 0


def test_warning_threshold_is_inclusive_and_evictions_are_ordered_and_compacted() -> None:
    calls: list[str] = []
    sizes = iter([80, 101, 90, 20])

    budget = CacheBudget(max_bytes=100, warning_percent=80)
    first = budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert first.warning_active is True
    assert first.warning_triggered is True
    assert first.compacted is False
    assert first.retained_bytes == 80
    assert calls == []

    compact_budget = CacheBudget(max_bytes=80, warning_percent=80)
    second = compact_budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert second.warning_active is False
    assert second.warning_triggered is True
    assert second.compacted is True
    assert second.utilization_percent == 25.0
    assert second.peak_utilization_percent == 126.25
    assert second.warning_triggered is True
    assert second.retained_bytes == 20
    assert calls == ["auxiliary", "navigation"]


@pytest.mark.parametrize(
    ("max_bytes", "warning_percent"),
    [
        (0, 80),
        (-1, 80),
        (100, 0),
        (100, 101),
        (100, -10),
    ],
)
def test_cache_budget_rejects_invalid_configuration(max_bytes: int, warning_percent: int) -> None:
    with pytest.raises(ValueError):
        CacheBudget(max_bytes=max_bytes, warning_percent=warning_percent)


def test_cache_warning_reports_peak_and_compaction_action_when_compacted() -> None:
    result = BudgetResult(
        retained_bytes=20,
        utilization_percent=20.0,
        peak_utilization_percent=126.3,
        warning_active=False,
        warning_triggered=True,
        compacted=True,
    )

    assert cache_warning(result, max_bytes=100) == (
        "Warning: Delphi cache peaked at 126.3% of the 100 byte budget; 20 bytes remain retained after compaction. "
        "Cache compacted. "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )


def test_cache_warning_reports_current_retention_without_compaction() -> None:
    result = BudgetResult(
        retained_bytes=80,
        utilization_percent=80.0,
        peak_utilization_percent=130.0,
        warning_active=True,
        warning_triggered=True,
        compacted=False,
    )

    assert cache_warning(result, max_bytes=100) == (
        "Warning: Delphi cache currently at 80.0% of the 100 byte budget; 80 bytes retained. "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )


def test_cache_warning_empty_when_threshold_not_reached() -> None:
    result = BudgetResult(
        retained_bytes=20,
        utilization_percent=20.0,
        peak_utilization_percent=50.0,
        warning_active=False,
        warning_triggered=False,
        compacted=False,
    )

    assert cache_warning(result, max_bytes=100) == ""
