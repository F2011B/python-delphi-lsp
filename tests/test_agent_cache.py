from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import textwrap

from delphi_lsp.agent_cache import estimate_deep_size
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
