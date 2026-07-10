from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import textwrap

import pytest

import delphi_lsp.agent_relations as agent_relations
from delphi_lsp.agent_context import AgentContext
from delphi_lsp.agent_protocol import AgentProtocolError, AgentResponse
from delphi_lsp.project_indexer import ProjectIndexResult, ProjectProblem, ProjectProblemType
from delphi_lsp.semantic import SymbolKind


def write_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")


def result_items(response: AgentResponse) -> list[dict[str, object]]:
    assert isinstance(response.result, list)
    return response.result


def find_cards(context: AgentContext, name: str) -> list[dict[str, object]]:
    return [
        item
        for item in result_items(context.handle({"action": "find", "query": name, "max_items": 50}))
        if item.get("name") == name
    ]


def find_cards_for_project(
    context: AgentContext,
    project_id: str,
    name: str,
) -> list[dict[str, object]]:
    return [
        item
        for item in result_items(
            context.handle(
                {"action": "find", "project_id": project_id, "query": name, "max_items": 50}
            )
        )
        if item.get("name") == name
    ]


def relation_items(response: AgentResponse) -> list[dict[str, object]]:
    return [item for item in result_items(response) if item.get("item_type") == "relation"]


def metadata_item(response: AgentResponse) -> dict[str, object]:
    return next(item for item in result_items(response) if item.get("item_type") == "relation_metadata")


def test_true_references_callers_and_callees_ignore_text_and_bare_values(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Api in 'Api.pas', Consumer in 'Consumer.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "Api.pas",
        """
        unit Api;
        interface
        procedure Called;
        procedure Bare;
        implementation
        procedure Called;
        begin
        end;
        procedure Bare;
        begin
        end;
        end.
        """,
    )
    write_source(
        tmp_path / "Consumer.pas",
        """
        unit Consumer;
        interface
        procedure Caller;
        implementation
        uses Api;
        procedure Caller;
        var S: string;
        begin
          // Called() and Bare() are not references.
          S := 'Called() Bare()';
          Called();
          Bare;
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    called = find_cards(context, "Called")[0]
    bare = find_cards(context, "Bare")[0]
    caller = find_cards(context, "Caller")[-1]

    references = context.handle(
        {"action": "trace", "relation": "references", "target_id": called["target_id"]}
    )
    callers = context.handle(
        {"action": "trace", "relation": "callers", "target_id": called["target_id"]}
    )
    callees = context.handle(
        {"action": "trace", "relation": "callees", "target_id": caller["target_id"]}
    )
    bare_callers = context.handle(
        {"action": "trace", "relation": "callers", "target_id": bare["target_id"]}
    )

    assert [item["name"] for item in relation_items(callers)] == ["Caller"]
    assert [item["name"] for item in relation_items(callees)] == ["Called"]
    assert relation_items(bare_callers) == []
    assert len(relation_items(references)) == 1
    evidence = relation_items(references)[0]["evidence"]
    assert evidence["path"] == "Consumer.pas"
    assert evidence["kind"] == "call"
    serialized = json.dumps(result_items(references), ensure_ascii=False)
    assert "Called() and Bare()" not in serialized
    assert "'Called() Bare()'" not in serialized
    assert metadata_item(callers)["completeness"] == "sound_partial"


def test_call_ownership_stops_at_unmapped_routines_and_program_bodies(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Api in 'Api.pas', Work in 'Work.pas';
        begin
          Target();
        end.
        """,
    )
    write_source(
        tmp_path / "Api.pas",
        """
        unit Api;
        interface
        procedure Target;
        implementation
        procedure Target;
        begin
        end;
        end.
        """,
    )
    write_source(
        tmp_path / "Work.pas",
        """
        unit Work;
        interface
        procedure Outer;
        procedure Caller;
        implementation
        uses Api;
        procedure Outer;
          procedure Inner;
          begin
            Target();
          end;
        begin
          Inner;
        end;
        procedure Caller;
        begin
          Target();
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = find_cards(context, "Target")[0]

    callers = context.handle(
        {"action": "trace", "relation": "callers", "target_id": target["target_id"]}
    )

    assert [item["name"] for item in relation_items(callers)] == ["Caller"]


def test_grouped_and_split_parameters_share_a_canonical_signature(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Signatures.pas",
        """
        unit Signatures;
        interface
        procedure Run(A, B: Integer);
        procedure Update(var Value: Integer); overload;
        procedure Update(const Value: Integer); overload;
        procedure Caller;
        implementation
        procedure Run(A: Integer; B: Integer);
        begin
        end;
        procedure Update(var Value: Integer);
        begin
        end;
        procedure Update(const Value: Integer);
        begin
        end;
        procedure Caller;
        begin
          Run(1, 2);
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    runs = find_cards(context, "Run")
    grouped_declaration = runs[0]

    grouped_callers = context.handle(
        {"action": "trace", "relation": "callers", "target_id": grouped_declaration["target_id"]}
    )
    split_callees = context.handle(
        {"action": "trace", "relation": "callees", "target_id": find_cards(context, "Caller")[1]["target_id"]}
    )

    assert [item["name"] for item in relation_items(grouped_callers)] == ["Caller"]
    assert [item["name"] for item in relation_items(split_callees)] == ["Run"]
    assert metadata_item(grouped_callers)["ambiguous_references"] == 0

    identity = agent_relations._target_signature_identity
    signatures = {
        "grouped": "(#2:integer)",
        "split": "(#1:integer;#1:integer)",
        "three": "(#3:integer)",
        "var": "(var#1:integer)",
        "const": "(const#1:integer)",
        "string": "(#2:string)",
        "nested_cdecl": "(#1:procedure(#2:integer)|cc:cdecl)",
        "nested_stdcall": "(#1:procedure(#2:integer)|cc:stdcall)",
    }
    target = lambda signature: agent_relations.RelationTarget(
        target_id=signature,
        source_path="Signatures.pas",
        path="Signatures.pas",
        unit_id="Signatures",
        unit_name="Signatures",
        name="Run",
        qualified_name="Signatures.Run",
        kind=SymbolKind.PROCEDURE.value,
        signature=signature,
        line=1,
        column=1,
        card={},
    )

    assert identity(target(signatures["grouped"])) == identity(target(signatures["split"]))
    assert identity(target(signatures["grouped"])) != identity(target(signatures["three"]))
    assert identity(target(signatures["var"])) != identity(target(signatures["const"]))
    assert identity(target(signatures["grouped"])) != identity(target(signatures["string"]))
    assert identity(target(signatures["nested_cdecl"])) != identity(target(signatures["nested_stdcall"]))
    assert identity(target(signatures["nested_cdecl"])) == "(:procedure(:integer,:integer)|cc:cdecl)"


def test_include_declared_targets_map_by_canonical_identity_after_source_map_shift(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses IncludeUnit in 'IncludeUnit.pas';
        begin
        end.
        """,
    )
    write_source(tmp_path / "api.inc", "procedure Included;")
    write_source(
        tmp_path / "IncludeUnit.pas",
        """
        unit IncludeUnit;
        interface
        {$I api.inc}
        procedure Caller;
        implementation
        procedure Included;
        begin
        end;
        procedure Caller;
        begin
          Included();
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    included = find_cards(context, "Included")[0]

    callers = context.handle(
        {"action": "trace", "relation": "callers", "target_id": included["target_id"]}
    )

    assert [item["name"] for item in relation_items(callers)] == ["Caller"]
    assert relation_items(callers)[0]["evidence"]["path"] == "IncludeUnit.pas"


def test_counterparts_share_reference_group_and_overloads_are_not_guessed(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Overloads.pas",
        """
        unit Overloads;
        interface
        procedure Run(Value: Integer); overload;
        procedure Run(Value: string); overload;
        procedure Invoke;
        implementation
        procedure Run(Value: Integer);
        begin
        end;
        procedure Run(Value: string);
        begin
        end;
        procedure Invoke;
        begin
          Run(1);
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    runs = sorted(find_cards(context, "Run"), key=lambda card: int(card["line"]))
    assert len(runs) == 4

    first = context.handle(
        {"action": "trace", "relation": "references", "target_id": runs[0]["target_id"]}
    )
    implementation = context.handle(
        {"action": "trace", "relation": "references", "target_id": runs[2]["target_id"]}
    )

    assert relation_items(first) == relation_items(implementation) == []
    first_meta = metadata_item(first)
    assert int(first_meta["ambiguous_references"]) >= 1
    assert first_meta["completeness"] == "sound_partial"


def test_canonical_signature_fallback_does_not_collapse_overloads(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Fallback.pas",
        """
        unit Fallback;
        interface
        procedure Run(Value: Integer); overload;
        procedure Run(Value: string); overload;
        implementation
        procedure Run(Value: Integer); begin end;
        procedure Run(Value: string); begin end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    runs = find_cards(context, "Run")
    context.handle(
        {"action": "trace", "relation": "references", "target_id": runs[0]["target_id"]}
    )
    relation_index = context._relation_index
    assert relation_index is not None
    relation_index._exact_targets.clear()
    relation_index._position_targets.clear()
    relation_index._line_targets.clear()
    relation_index._symbol_targets.clear()

    deep_runs = sorted(
        {
            id(symbol): symbol
            for model in relation_index._semantics.models.values()
            for symbols in model.unit_scope.symbols.values()
            for symbol in symbols
            if symbol.name == "Run"
        }.values(),
        key=lambda symbol: symbol.decl_range.start_line,
    )
    mapped = [relation_index._target_for_symbol(symbol) for symbol in deep_runs]
    shifted_source_symbol = replace(
        deep_runs[1],
        decl_range=replace(
            deep_runs[1].decl_range,
            file_name=str(tmp_path / "virtual-include.inc"),
            start_line=999,
            start_col=1,
        ),
    )
    shifted_source_target = relation_index._target_for_symbol(shifted_source_symbol)

    assert [target.signature for target in mapped if target is not None] == [
        "(#1:integer)",
        "(#1:string)",
        "(#1:integer)",
        "(#1:string)",
    ]
    assert shifted_source_target is not None
    assert shifted_source_target.signature == "(#1:string)"


def test_canonical_fallback_preserves_calling_conventions_or_omits_ambiguity(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Conventions.pas",
        """
        unit Conventions;
        interface
        procedure Execute(Value: Integer); cdecl; overload;
        procedure Execute(Value: Integer); stdcall; overload;
        implementation
        procedure Execute(Value: Integer); cdecl; begin end;
        procedure Execute(Value: Integer); stdcall; begin end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = find_cards(context, "Execute")
    context.handle(
        {"action": "trace", "relation": "references", "target_id": cards[0]["target_id"]}
    )
    relation_index = context._relation_index
    assert relation_index is not None
    relation_index._exact_targets.clear()
    relation_index._position_targets.clear()
    relation_index._line_targets.clear()
    relation_index._symbol_targets.clear()
    deep_symbols = sorted(
        [
            symbol
            for model in relation_index._semantics.models.values()
            for symbols in model.unit_scope.symbols.values()
            for symbol in symbols
            if symbol.name == "Execute"
        ],
        key=lambda symbol: symbol.decl_range.start_line,
    )

    mapped = [relation_index._target_for_symbol(symbol) for symbol in deep_symbols]

    assert mapped[0] is None
    assert mapped[1] is None
    assert mapped[2] is not None and mapped[2].signature.endswith("|cc:cdecl")
    assert mapped[3] is not None and mapped[3].signature.endswith("|cc:stdcall")


def test_canonical_fallback_preserves_parameter_modes(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Modes.pas",
        """
        unit Modes;
        interface
        procedure Update(var Value: Integer); overload;
        procedure Update(const Value: Integer); overload;
        implementation
        procedure Update(var Value: Integer); begin end;
        procedure Update(const Value: Integer); begin end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = find_cards(context, "Update")
    context.handle(
        {"action": "trace", "relation": "references", "target_id": cards[0]["target_id"]}
    )
    relation_index = context._relation_index
    assert relation_index is not None
    relation_index._exact_targets.clear()
    relation_index._position_targets.clear()
    relation_index._line_targets.clear()
    relation_index._symbol_targets.clear()
    deep_symbols = sorted(
        [
            symbol
            for model in relation_index._semantics.models.values()
            for symbols in model.unit_scope.symbols.values()
            for symbol in symbols
            if symbol.name == "Update"
        ],
        key=lambda symbol: symbol.decl_range.start_line,
    )

    mapped = [relation_index._target_for_symbol(symbol) for symbol in deep_symbols]

    assert mapped[0] is not None and mapped[0].signature.startswith("(var#1:")
    assert mapped[1] is not None and mapped[1].signature.startswith("(const#1:")
    assert mapped[2] is not None and mapped[2].signature.startswith("(var#1:")
    assert mapped[3] is not None and mapped[3].signature.startswith("(const#1:")


def test_uses_contains_inheritance_and_implements_are_project_scoped(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Bundle.dpk",
        """
        package Bundle;
        contains
          Types in 'Types.pas',
          Consumer in 'Consumer.pas';
        end.
        """,
    )
    write_source(
        tmp_path / "Base.pas",
        """
        unit Base;
        interface
        type
          IBase = interface
          end;
          IChild = interface(IBase)
          end;
          TBase = class
          end;
          TGeneric<T> = class
          end;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "Types.pas",
        """
        unit Types;
        interface
        uses Base;
        type
          TChild = class(Base.TBase)
          end;
          TGenericChild = class(Base.TGeneric<Integer>)
          end;
          TImplementation = class(Base.TBase, Base.IBase)
          end;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "Consumer.pas",
        """
        unit Consumer;
        interface
        uses Types, MissingUnit;
        implementation
        uses Base;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)

    bundle = find_cards(context, "Bundle")[0]
    types = find_cards(context, "Types")[0]
    base = find_cards(context, "Base")[0]
    consumer = find_cards(context, "Consumer")[0]
    child = find_cards(context, "TChild")[0]
    generic_child = find_cards(context, "TGenericChild")[0]
    implementation = find_cards(context, "TImplementation")[0]
    interface_child = find_cards(context, "IChild")[0]

    bundle_uses = context.handle(
        {"action": "trace", "relation": "uses", "target_id": bundle["target_id"], "max_items": 50}
    )
    consumer_uses = context.handle(
        {"action": "trace", "relation": "uses", "target_id": consumer["target_id"], "max_items": 50}
    )
    base_used_by = context.handle(
        {"action": "trace", "relation": "used_by", "target_id": base["target_id"], "max_items": 50}
    )

    assert {item["name"] for item in relation_items(bundle_uses)} == {"Types", "Consumer"}
    assert {item["name"] for item in relation_items(consumer_uses)} == {"Types", "Base"}
    assert {item["name"] for item in relation_items(base_used_by)} == {"Types", "Consumer"}
    assert any(item.get("item_type") == "relation_problem" and "MissingUnit" in str(item) for item in result_items(consumer_uses))

    assert [item["name"] for item in relation_items(context.handle(
        {"action": "trace", "relation": "inherits", "target_id": child["target_id"]}
    ))] == ["TBase"]
    assert [item["name"] for item in relation_items(context.handle(
        {"action": "trace", "relation": "inherits", "target_id": generic_child["target_id"]}
    ))] == ["TGeneric"]
    assert [item["name"] for item in relation_items(context.handle(
        {"action": "trace", "relation": "inherits", "target_id": interface_child["target_id"]}
    ))] == ["IBase"]
    assert [item["name"] for item in relation_items(context.handle(
        {"action": "trace", "relation": "implements", "target_id": implementation["target_id"]}
    ))] == ["IBase"]
    assert types["path"] == "Types.pas"


@pytest.mark.parametrize(
    ("source_kind", "target_kind", "expected"),
    [
        (SymbolKind.CLASS, SymbolKind.CLASS, "inherits"),
        (SymbolKind.INTERFACE, SymbolKind.INTERFACE, "inherits"),
        (SymbolKind.CLASS, SymbolKind.INTERFACE, "implements"),
        (SymbolKind.RECORD, SymbolKind.INTERFACE, "implements"),
        (SymbolKind.RECORD, SymbolKind.CLASS, None),
    ],
)
def test_type_relation_classification_includes_record_interface_implementation(
    source_kind: SymbolKind,
    target_kind: SymbolKind,
    expected: str | None,
) -> None:
    assert agent_relations._classify_type_relation(source_kind, target_kind) == expected


def test_deep_index_is_lazy_reused_and_invalidated_by_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Work in 'Work.pas';
        begin
        end.
        """,
    )
    source = tmp_path / "Work.pas"
    write_source(
        source,
        """
        unit Work;
        interface
        procedure Target;
        procedure Caller;
        implementation
        procedure Target; begin end;
        procedure Caller; begin Target(); end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = find_cards(context, "Target")[0]
    caller = find_cards(context, "Caller")[-1]

    real_indexer = agent_relations.ProjectIndexer
    index_calls: list[str] = []
    parse_calls: list[str] = []
    real_parse = agent_relations.DelphiParser.parse

    class RecordingIndexer(real_indexer):
        def index(self, file_name: str):
            index_calls.append(file_name)
            return super().index(file_name)

    monkeypatch.setattr(agent_relations, "ProjectIndexer", RecordingIndexer)

    def recording_parse(parser, text: str, file_name: str, **kwargs):
        parse_calls.append(file_name)
        return real_parse(parser, text, file_name, **kwargs)

    monkeypatch.setattr(agent_relations.DelphiParser, "parse", recording_parse)

    context.handle({"action": "inspect", "target_id": target["target_id"]})
    assert index_calls == []
    context.handle({"action": "trace", "relation": "callers", "target_id": target["target_id"]})
    first_deep_parse_calls = list(parse_calls)
    context.handle({"action": "trace", "relation": "callees", "target_id": caller["target_id"]})
    assert len(index_calls) == 1
    assert sorted(Path(path).name for path in first_deep_parse_calls) == ["Main.dpr", "Work.pas"]
    assert parse_calls == first_deep_parse_calls

    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    refreshed_target = find_cards(context, "Target")[0]
    before_second_deep_parse = len(parse_calls)
    context.handle(
        {"action": "trace", "relation": "callers", "target_id": refreshed_target["target_id"]}
    )
    assert len(index_calls) == 2
    assert sorted(Path(path).name for path in parse_calls[before_second_deep_parse:]) == [
        "Main.dpr",
        "Work.pas",
    ]


def test_fallback_workspace_deep_parses_each_selected_unit_once_and_reuses_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(
        tmp_path / "A.pas",
        """
        unit A;
        interface
        uses B;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "B.pas",
        """
        unit B;
        interface
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    a = find_cards(context, "A")[0]
    b = find_cards(context, "B")[0]
    real_parser = agent_relations.DelphiParser
    parse_calls: list[str] = []

    class RecordingParser(real_parser):
        def parse(self, text: str, file_name: str, **kwargs):
            parse_calls.append(file_name)
            return super().parse(text, file_name, **kwargs)

    monkeypatch.setattr(agent_relations, "DelphiParser", RecordingParser)

    uses = context.handle({"action": "trace", "relation": "uses", "target_id": a["target_id"]})
    context.handle({"action": "trace", "relation": "used_by", "target_id": b["target_id"]})

    assert [item["name"] for item in relation_items(uses)] == ["B"]
    assert sorted(Path(path).name for path in parse_calls) == ["A.pas", "B.pas"]


def test_deep_load_problem_does_not_expose_external_exception_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(
        tmp_path / "Leak.pas",
        """
        unit Leak;
        interface
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    leak = find_cards(context, "Leak")[0]
    external_path = "/private/secret/Outside.pas"

    def fail_read(_path: Path) -> str:
        raise FileNotFoundError(2, "No such file or directory", external_path)

    monkeypatch.setattr(agent_relations, "read_source_text", fail_read)

    response = context.handle(
        {"action": "trace", "relation": "uses", "target_id": leak["target_id"]}
    )
    serialized = json.dumps(result_items(response), ensure_ascii=False)
    problem = next(
        item for item in result_items(response) if item.get("item_type") == "relation_problem"
    )

    assert external_path not in serialized
    assert problem["path"] == "Leak.pas"
    assert "FileNotFoundError" in str(problem["message"])


def test_project_deep_load_problem_sanitizes_known_external_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        begin
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    main = find_cards(context, "Main")[0]
    external_path = "/private/secret/Outside.pas"
    description = (
        'Dependency load failed: FileNotFoundError(2, "No such file", '
        f'"{external_path}") while parsing UsesClause.'
    )

    def leaking_index(_indexer: agent_relations.ProjectIndexer, _file_name: str) -> ProjectIndexResult:
        return ProjectIndexResult(
            parsed_units=[],
            include_files=[],
            problems=[
                ProjectProblem(
                    problem_type=ProjectProblemType.CANT_OPEN_FILE,
                    file_name=external_path,
                    description=description,
                )
            ],
            not_found_units=[],
        )

    monkeypatch.setattr(agent_relations.ProjectIndexer, "index", leaking_index)

    response = context.handle(
        {"action": "trace", "relation": "uses", "target_id": main["target_id"]}
    )
    serialized = json.dumps(result_items(response), ensure_ascii=False)
    problem = next(
        item for item in result_items(response) if item.get("item_type") == "relation_problem"
    )
    safe_path = "@external/relation/Outside.pas"

    assert external_path not in serialized
    assert problem == {
        "item_type": "relation_problem",
        "kind": "cant_open_file",
        "message": (
            'Dependency load failed: FileNotFoundError(2, "No such file", '
            f'"{safe_path}") while parsing UsesClause.'
        ),
        "path": safe_path,
    }


def test_display_project_path_sanitizes_cross_platform_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path
    inside_root = PurePosixPath(f"{tmp_path}/Source/Main.pas")
    outside_posix = PurePosixPath("/private/secret/Outside.pas")
    outside_windows = PureWindowsPath(r"C:\private\secret\Outside.pas")
    windows_root = Path("C:/workspace-root")
    windows_inside = PureWindowsPath("C:/workspace-root/Source/Main.pas")

    assert agent_relations._display_project_path(inside_root, root) == "Source/Main.pas"
    assert agent_relations._display_project_path(outside_posix, root) == "@external/relation/Outside.pas"
    assert agent_relations._display_project_path(outside_windows, root) == "@external/relation/Outside.pas"
    assert agent_relations._display_project_path(windows_inside, windows_root) == "Source/Main.pas"


def test_safe_problem_message_sanitizes_cross_platform_absolute_references(tmp_path: Path) -> None:
    root = tmp_path
    message = (
        "Dependency load failed: /private/secret/Outside.pas and C:\\private\\secret\\Outside.pas"
    )
    safe = agent_relations._safe_problem_message(
        message,
        root,
        (
            PurePosixPath("/private/secret/Outside.pas"),
            PureWindowsPath(r"C:\private\secret\Outside.pas"),
        ),
    )

    assert "/private/secret/Outside.pas" not in safe
    assert "C:\\private\\secret\\Outside.pas" not in safe
    assert safe.count("@external/relation/Outside.pas") == 2


@pytest.mark.parametrize(
    ("known_path", "diagnostic_path"),
    (
        (
            PureWindowsPath(r"C:\private\secret\Outside.pas"),
            "C:/private/secret/Outside.pas",
        ),
        (
            PureWindowsPath(r"\\server\share\secret\Outside.pas"),
            "//server/share/secret/Outside.pas",
        ),
        (
            "//server/share/secret/Outside.pas",
            r"\\server\share\secret\Outside.pas",
        ),
    ),
)
def test_safe_problem_message_sanitizes_equivalent_path_spellings(
    tmp_path: Path,
    known_path: str | PureWindowsPath,
    diagnostic_path: str,
) -> None:
    assert agent_relations._safe_problem_message(
        diagnostic_path,
        tmp_path,
        (known_path,),
    ) == "@external/relation/Outside.pas"


@pytest.mark.parametrize(
    ("known_path", "diagnostic_path"),
    (
        (
            PureWindowsPath(r"C:\private\secret\Outside.pas"),
            r"C:\private/secret\Outside.pas",
        ),
        (
            PureWindowsPath(r"C:\private\secret\Outside.pas"),
            r"C:/private\secret/Outside.pas",
        ),
        (
            PureWindowsPath(r"C:\private\secret\Outside.pas"),
            r"c:/PRIVATE\Secret/outside.PAS",
        ),
        (
            PureWindowsPath(r"\\server\share\secret\Outside.pas"),
            r"\\server/share\secret/Outside.pas",
        ),
        (
            PureWindowsPath(r"\\server\share\secret\Outside.pas"),
            r"//server\share/secret\Outside.pas",
        ),
    ),
)
def test_safe_problem_message_sanitizes_mixed_path_separators(
    tmp_path: Path,
    known_path: PureWindowsPath,
    diagnostic_path: str,
) -> None:
    assert agent_relations._safe_problem_message(
        diagnostic_path,
        tmp_path,
        (known_path,),
    ) == "@external/relation/Outside.pas"


def test_safe_problem_message_preserves_posix_path_before_windows_parsing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SimulatedWindowsPath(PureWindowsPath):
        def expanduser(self):
            return self

        def resolve(self):
            return self

    external_path = "/private/secret/Outside.pas"
    monkeypatch.setattr(agent_relations, "Path", SimulatedWindowsPath)

    assert (
        agent_relations._safe_problem_message(external_path, tmp_path, (external_path,))
        == "@external/relation/Outside.pas"
    )


def test_project_problem_item_preserves_posix_path_before_windows_parsing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SimulatedWindowsPath(PureWindowsPath):
        def expanduser(self):
            return self

        def resolve(self):
            return self

    class ProblemType:
        value = "cant_open_file"

    class Problem:
        problem_type = ProblemType()
        file_name = "/private/secret/Outside.pas"
        description = "failure"

    monkeypatch.setattr(agent_relations, "Path", SimulatedWindowsPath)

    assert agent_relations._project_problem_item(Problem(), tmp_path)["path"] == (
        "@external/relation/Outside.pas"
    )


def test_relation_index_stays_inside_the_selected_project(tmp_path: Path) -> None:
    write_source(
        tmp_path / "First.dpr",
        """
        program First;
        uses FirstUnit in 'first/FirstUnit.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "first" / "FirstUnit.pas",
        """
        unit FirstUnit;
        interface
        uses Shared in '../Shared.pas';
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "Second.dpr",
        """
        program Second;
        uses SecondUnit in 'second/SecondUnit.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "second" / "SecondUnit.pas",
        """
        unit SecondUnit;
        interface
        uses Shared in '../Shared.pas';
        implementation
        end.
        """,
    )
    write_source(tmp_path / "Shared.pas", "unit Shared; interface implementation end.")
    context = AgentContext.open(tmp_path)
    project_ids = {project.name: project.project_id for project in context.workspace.projects}

    first = find_cards_for_project(context, project_ids["First"], "FirstUnit")[0]
    first_uses = context.handle(
        {
            "action": "trace",
            "project_id": project_ids["First"],
            "relation": "uses",
            "target_id": first["target_id"],
        }
    )
    assert {item["name"] for item in relation_items(first_uses)} == {"Shared"}

    second = find_cards_for_project(context, project_ids["Second"], "SecondUnit")[0]
    second_uses = context.handle(
        {
            "action": "trace",
            "project_id": project_ids["Second"],
            "relation": "uses",
            "target_id": second["target_id"],
        }
    )
    assert {item["name"] for item in relation_items(second_uses)} == {"Shared"}
    assert all(item.get("name") != "FirstUnit" for item in result_items(second_uses))


def test_malformed_deep_unit_yields_partial_relations_and_problem(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Good in 'Good.pas', Broken in 'Broken.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "Good.pas",
        """
        unit Good;
        interface
        procedure Available;
        implementation
        procedure Available;
        begin
        end;
        end.
        """,
    )
    write_source(
        tmp_path / "Broken.pas",
        """
        unit Broken;
        interface
        procedure BrokenRoutine;
        implementation
        procedure BrokenRoutine;
        begin
          if then
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    main = find_cards(context, "Main")[0]

    response = context.handle(
        {"action": "trace", "relation": "uses", "target_id": main["target_id"], "max_items": 50}
    )

    assert "Good" in {item["name"] for item in relation_items(response)}
    assert any(item.get("item_type") == "relation_problem" and "Broken.pas" in str(item) for item in result_items(response))
    assert metadata_item(response)["deep_problem_count"]


def test_trace_focus_pagination_cursor_guards_and_no_source_text(tmp_path: Path) -> None:
    caller_declarations = "\n".join(f"procedure Caller{index};" for index in range(6))
    caller_implementations = "\n".join(
        f"procedure Caller{index}; begin Target(); end;" for index in range(6)
    )
    write_source(
        tmp_path / "Many.pas",
        f"""
        unit Many;
        interface
        procedure Target;
        {caller_declarations}
        implementation
        procedure Target; begin end;
        {caller_implementations}
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = find_cards(context, "Target")[0]
    context.handle({"action": "focus", "target_id": target["target_id"]})
    request = {"action": "trace", "relation": "callers", "max_items": 2, "max_chars": 12000}

    first = context.handle(request)
    assert first.page.truncated
    assert first.page.next_cursor
    second = context.handle({**request, "cursor": first.page.next_cursor})
    assert second.page.returned
    assert first.focus.target_id == target["target_id"]
    assert all(
        key not in {"text", "source", "body", "declaration"}
        for item in [*result_items(first), *result_items(second)]
        for key in item
    )

    with pytest.raises(AgentProtocolError) as mismatch:
        context.handle(
            {
                "action": "trace",
                "relation": "references",
                "cursor": first.page.next_cursor,
                "max_items": 2,
            }
        )
    assert mismatch.value.code == "cursor_mismatch"

    (tmp_path / "Many.pas").write_text(
        (tmp_path / "Many.pas").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentProtocolError) as stale:
        context.handle({**request, "cursor": first.page.next_cursor})
    assert stale.value.code == "stale_cursor"


def test_trace_metadata_is_on_first_page_when_results_are_truncated(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Paged.pas",
        """
        unit Paged;
        interface
        procedure Target;
        procedure ZuluCaller;
        procedure AlphaCaller;
        implementation
        procedure Target; begin end;
        procedure ZuluCaller; begin Target(); end;
        procedure AlphaCaller; begin Target(); end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = find_cards(context, "Target")[0]
    request = {
        "action": "trace",
        "relation": "callers",
        "target_id": target["target_id"],
        "max_items": 1,
        "max_chars": 12000,
    }

    first = context.handle(request)
    repeated_first = context.handle(request)
    assert [item["item_type"] for item in result_items(first)] == ["relation_metadata"]
    assert metadata_item(first)["completeness"] == "sound_partial"
    assert first.page.truncated
    assert first.page.next_cursor == repeated_first.page.next_cursor
    assert result_items(first) == result_items(repeated_first)

    relation_names: list[str] = []
    cursor = first.page.next_cursor
    while cursor:
        page = context.handle({**request, "cursor": cursor})
        relation_names.extend(item["name"] for item in relation_items(page))
        cursor = page.page.next_cursor

    assert relation_names == ["AlphaCaller", "ZuluCaller"]


def test_trace_validation_and_relation_applicability(tmp_path: Path) -> None:
    write_source(tmp_path / "Main.dpr", "program Main; begin end.")
    context = AgentContext.open(tmp_path)
    main = find_cards(context, "Main")[0]

    with pytest.raises(AgentProtocolError) as relation:
        context.handle({"action": "trace", "target_id": main["target_id"]})
    assert relation.value.code == "relation_required"

    with pytest.raises(AgentProtocolError) as target:
        context.handle({"action": "trace", "relation": "references"})
    assert target.value.code == "target_required"

    with pytest.raises(AgentProtocolError) as missing:
        context.handle({"action": "trace", "relation": "references", "target_id": "missing"})
    assert missing.value.code == "target_not_found"

    with pytest.raises(AgentProtocolError) as not_applicable:
        context.handle({"action": "trace", "relation": "implements", "target_id": main["target_id"]})
    assert not_applicable.value.code == "relation_not_applicable"
