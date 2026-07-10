from __future__ import annotations

import json
from pathlib import Path
import textwrap
import unicodedata

import pytest

import delphiast.agent_context as agent_context_module
from delphiast.agent_context import AgentContext
from delphiast.agent_protocol import AgentProtocolError, AgentRequest, AgentResponse, Focus


def write_source(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")


def result_items(response: AgentResponse) -> list[dict[str, object]]:
    assert isinstance(response.result, list)
    return response.result


def card_named(response: AgentResponse, name: str) -> dict[str, object]:
    return next(item for item in result_items(response) if item.get("name") == name)


def assert_budget(response: AgentResponse, max_chars: int) -> None:
    serialized = json.dumps(
        response.result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert response.context.chars == len(serialized)
    assert response.context.approx_tokens == (len(serialized) + 3) // 4
    assert response.context.chars <= max_chars


def test_open_exposes_read_only_workspace_and_complete_response_envelope(tmp_path: Path) -> None:
    write_source(tmp_path / "Main.dpr", "program Main; begin end.")

    context = AgentContext.open(tmp_path)
    response = context.handle({"action": "open"})

    assert isinstance(response, AgentResponse)
    assert context.workspace.active_project_id
    assert response.workspace_revision == context.workspace.workspace_revision
    assert response.focus == Focus(project_id=context.workspace.active_project_id)
    assert any(item["item_type"] == "project" and item["active"] for item in result_items(response))
    assert any(item["item_type"] == "unit" and item["name"] == "Main" for item in result_items(response))
    assert_budget(response, 12000)
    with pytest.raises(AttributeError):
        context.workspace = object()  # type: ignore[misc]


def test_multi_project_symbol_actions_require_selection_and_switch_projects(tmp_path: Path) -> None:
    write_source(
        tmp_path / "A.dpr",
        """
        program A;
        uses AOnly in 'a/AOnly.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "a" / "AOnly.pas",
        """
        unit AOnly;
        interface
        type
          TAOnly = class
          end;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "B.dpr",
        """
        program B;
        uses BOnly in 'b/BOnly.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "b" / "BOnly.pas",
        """
        unit BOnly;
        interface
        type
          TBOnly = class
          end;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    project_ids = {project.name: project.project_id for project in context.workspace.projects}

    assert context.workspace.active_project is None
    with pytest.raises(AgentProtocolError) as caught:
        context.handle({"action": "find", "query": "Only"})
    assert caught.value.code == "project_required"

    a_result = context.handle({"action": "find", "project_id": project_ids["A"], "query": "TAOnly"})
    a_target = card_named(a_result, "TAOnly")
    focused = context.handle(
        {
            "action": "focus",
            "project_id": project_ids["A"],
            "target_id": str(a_target["target_id"]),
        }
    )
    assert focused.focus.project_id == project_ids["A"]
    assert focused.focus.unit_id == a_target["unit_id"]
    assert focused.focus.target_id == a_target["target_id"]

    b_result = context.handle({"action": "find", "project_id": project_ids["B"], "query": "Only"})
    assert "TBOnly" in [item["name"] for item in result_items(b_result)]
    assert all(not str(item["name"]).startswith("TA") for item in result_items(b_result))
    assert b_result.focus == Focus(project_id=project_ids["B"])


def test_find_uses_only_active_units_and_reads_each_original_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Selected in 'src/Selected.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "src" / "Selected.pas",
        """
        unit Selected;
        interface
        type
          TSelected = class
          end;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "Noise.pas",
        """
        unit Noise;
        interface
        type
          TNoise = class
          end;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    calls: list[tuple[str, str]] = []
    read_calls: list[Path] = []
    real_builder = agent_context_module.build_outline_semantic_model
    real_reader = agent_context_module.read_source_text

    def recording_builder(text: str, file_name: str):
        calls.append((text, file_name))
        return real_builder(text, file_name)

    def recording_reader(path: Path) -> str:
        read_calls.append(path)
        return real_reader(path)

    monkeypatch.setattr(agent_context_module, "build_outline_semantic_model", recording_builder)
    monkeypatch.setattr(agent_context_module, "read_source_text", recording_reader)

    selected = context.handle({"action": "find", "query": "TSelected"})
    noise = context.handle({"action": "find", "query": "TNoise"})
    context.handle({"action": "find", "query": "TSelected"})

    assert card_named(selected, "TSelected")["path"] == "src/Selected.pas"
    assert result_items(noise) == []
    assert sorted(Path(file_name).name for _, file_name in calls) == ["Main.dpr", "Selected.pas"]
    assert all(Path(file_name).is_absolute() for _, file_name in calls)
    assert sorted(path.name for path in read_calls) == ["Main.dpr", "Selected.pas"]
    assert all(path.is_absolute() for path in read_calls)


def test_find_ranks_exact_then_prefix_then_substring_and_has_stable_overload_ids(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Ranking.pas",
        """
        unit Ranking;
        interface
        procedure Run; overload;
        procedure Run(Value: Integer); overload;
        procedure Runner;
        procedure ExecuteRun;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)

    response = context.handle({"action": "find", "query": "Run", "max_items": 20})
    cards = result_items(response)

    assert [card["name"] for card in cards] == ["Run", "Run", "Runner", "ExecuteRun"]
    run_cards = [card for card in cards if card["name"] == "Run"]
    assert len({card["target_id"] for card in run_cards}) == 2
    assert [card["line"] for card in run_cards] == sorted(card["line"] for card in run_cards)
    assert all(
        set(card)
        == {
            "target_id",
            "unit_id",
            "name",
            "qualified_name",
            "kind",
            "path",
            "line",
            "column",
            "visibility",
            "owner",
            "type",
        }
        for card in cards
    )
    assert all("source" not in key and key not in {"text", "body", "declaration"} for card in cards for key in card)


def test_target_ids_are_stable_when_the_project_root_moves(tmp_path: Path) -> None:
    def make_project(root: Path) -> AgentContext:
        write_source(
            root / "src" / "Stable.pas",
            """
            unit Stable;
            interface
            type
              TStable = class
              end;
            implementation
            end.
            """,
        )
        return AgentContext.open(root)

    first = make_project(tmp_path / "first")
    second = make_project(tmp_path / "second")

    first_card = card_named(first.handle({"action": "find", "query": "TStable"}), "TStable")
    second_card = card_named(second.handle({"action": "find", "query": "TStable"}), "TStable")

    assert first_card["target_id"] == second_card["target_id"]
    assert first_card["unit_id"] == second_card["unit_id"]
    assert first_card["path"] == second_card["path"] == "src/Stable.pas"


def test_overloaded_target_ids_are_stable_when_the_project_root_moves(tmp_path: Path) -> None:
    source = """
        unit StableOverloads;
        interface
        procedure Run(Value: Integer); overload;
        procedure Run(Value: string); overload;
        implementation
        procedure Run(Value: Integer);
        begin
          IntegerBodyMarker;
        end;
        procedure Run(Value: string);
        begin
          StringBodyMarker;
        end;
        end.
    """

    def target_ids(root: Path) -> list[str]:
        write_source(root / "src" / "StableOverloads.pas", source)
        context = AgentContext.open(root)
        cards = [
            card
            for card in result_items(
                context.handle({"action": "find", "query": "Run", "max_items": 20})
            )
            if card["qualified_name"] == "StableOverloads.Run"
        ]
        return [str(card["target_id"]) for card in sorted(cards, key=lambda card: int(card["line"]))]

    first_ids = target_ids(tmp_path / "first")
    second_ids = target_ids(tmp_path / "second")

    assert first_ids == second_ids
    assert len(first_ids) == 4
    assert len(set(first_ids)) == 4


def test_handle_validates_agent_request_instances_and_trace_requires_a_relation(tmp_path: Path) -> None:
    write_source(tmp_path / "Main.dpr", "program Main; begin end.")
    context = AgentContext.open(tmp_path)

    with pytest.raises(AgentProtocolError) as invalid:
        context.handle(AgentRequest(action="find", max_items=0))
    assert invalid.value.code == "max_items_out_of_range"

    with pytest.raises(AgentProtocolError) as trace:
        context.handle({"action": "trace"})
    assert trace.value.code == "relation_required"


def _worker_context(tmp_path: Path) -> tuple[AgentContext, str, str]:
    write_source(
        tmp_path / "Worker.pas",
        """
        unit Worker;
        interface
        type
          TWorker = class
          private
            FValue: Integer;
          public
            procedure Run;
            property Value: Integer read FValue;
          end;
        implementation
        procedure TWorker.Run;
          type
            TLocalState = record
              case Byte of
                0: (Count: Integer);
                1: (Ready: Boolean);
            end;
          procedure LocalStep;
          begin
            FValue := FValue + 1;
          end;
        begin
          case FValue of
            0:
              begin
                LocalStep;
              end;
          end;
          FValue := FValue + 2;
          try
            LocalStep;
          finally
            FValue := FValue + 3;
          end;
          FValue := FValue + 4;
        end;

        procedure AfterWork;
        begin
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    type_card = card_named(context.handle({"action": "find", "query": "TWorker"}), "TWorker")
    run_cards = [
        item
        for item in result_items(context.handle({"action": "find", "query": "TWorker.Run", "max_items": 20}))
        if item["qualified_name"] == "Worker.TWorker.Run"
    ]
    declaration_card = min(run_cards, key=lambda card: int(card["line"]))
    return context, str(type_card["target_id"]), str(declaration_card["target_id"])


def test_inspect_summary_members_declaration_and_context_have_distinct_bounded_results(tmp_path: Path) -> None:
    context, type_target, _ = _worker_context(tmp_path)

    summary = context.handle({"action": "inspect", "target_id": type_target, "detail": "summary"})
    members = context.handle({"action": "inspect", "target_id": type_target, "detail": "members"})
    declaration = context.handle({"action": "inspect", "target_id": type_target, "detail": "declaration"})
    narrow_context = context.handle({"action": "inspect", "target_id": type_target, "detail": "context"})
    type_body = context.handle({"action": "inspect", "target_id": type_target, "detail": "body"})

    assert [item["name"] for item in result_items(summary)] == ["TWorker"]
    assert all("text" not in item for item in result_items(summary))
    assert [item["name"] for item in result_items(members)] == ["FValue", "Run", "Value"]
    declaration_items = result_items(declaration)
    assert declaration_items
    assert all(
        {"path", "start_line", "start_col", "end_line", "end_col", "text"} <= set(item)
        for item in declaration_items
    )
    assert "TWorker = class" in "".join(str(item["text"]) for item in declaration_items)
    assert result_items(narrow_context)[0]["target_id"] == type_target
    assert "TWorker = class" in "".join(
        str(item.get("text", "")) for item in result_items(narrow_context)
    )
    type_source = "".join(str(item["text"]) for item in result_items(type_body))
    assert type_source.startswith("TWorker = class")
    assert "property Value" in type_source
    assert type_source.rstrip().endswith("end;")
    assert "implementation" not in type_source
    assert_budget(declaration, 12000)
    assert_budget(narrow_context, 12000)


def test_body_uses_original_source_and_matches_outer_end_across_nested_case_try_and_routine(
    tmp_path: Path,
) -> None:
    context, _, run_target = _worker_context(tmp_path)

    response = context.handle(
        {
            "action": "inspect",
            "target_id": run_target,
            "detail": "body",
            "max_items": 50,
            "max_chars": 40000,
        }
    )
    body = "".join(str(item["text"]) for item in result_items(response))

    assert body.startswith("procedure TWorker.Run;")
    assert "procedure LocalStep;" in body
    assert "TLocalState = record" in body
    assert "1: (Ready: Boolean);" in body
    assert "FValue := FValue + 2;" in body
    assert "try" in body
    assert "FValue := FValue + 4;" in body
    assert "procedure AfterWork;" not in body
    assert body.rstrip().endswith("end;")
    assert result_items(context.handle({"action": "find", "query": "LocalStep"})) == []
    assert result_items(context.handle({"action": "find", "query": "TLocalState"})) == []


def test_enclosing_routine_span_skips_local_routine_and_class_forward_declarations(
    tmp_path: Path,
) -> None:
    write_source(
        tmp_path / "LocalForwards.dpr",
        """
        program LocalForwards;
        procedure Outer;
          type
            TLocalForward = class;
          procedure Inner; forward;
          procedure Inner;
          begin
            InnerBodyMarker;
          end;
        begin
          OuterBodyMarker;
          Inner;
        end;
        begin
          Outer;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = card_named(context.handle({"action": "find", "query": "Outer"}), "Outer")

    response = context.handle(
        {"action": "inspect", "target_id": target["target_id"], "detail": "body"}
    )
    body = "".join(str(item["text"]) for item in result_items(response))

    assert "TLocalForward = class;" in body
    assert "procedure Inner; forward;" in body
    assert "InnerBodyMarker" in body
    assert "OuterBodyMarker" in body
    assert body.rstrip().endswith("end;")
    assert "begin\n  Outer;" not in body
    assert result_items(context.handle({"action": "find", "query": "TLocalForward"})) == []
    assert result_items(context.handle({"action": "find", "query": "Inner"})) == []


@pytest.mark.parametrize(
    ("query", "qualified_name", "marker"),
    [
        ("TNumber.Add", "AdvancedOwners.TNumber.Add", "OperatorBodyMarker"),
        ("TBox<T>.SetValue", "AdvancedOwners.TBox<T>.SetValue", "GenericOwnerBodyMarker"),
    ],
)
def test_operator_and_generic_owner_declarations_match_their_implementations(
    tmp_path: Path,
    query: str,
    qualified_name: str,
    marker: str,
) -> None:
    write_source(
        tmp_path / "AdvancedOwners.pas",
        """
        unit AdvancedOwners;
        interface
        type
          TNumber = record
            class operator Add(const Left, Right: TNumber): TNumber;
          end;
          TBox<T> = class
            procedure SetValue(const Value: T);
          end;
        implementation
        class operator TNumber.Add(const Left, Right: TNumber): TNumber;
        begin
          OperatorBodyMarker;
        end;
        procedure TBox<T>.SetValue(const Value: T);
        begin
          GenericOwnerBodyMarker;
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = [
        card
        for card in result_items(
            context.handle({"action": "find", "query": query, "max_items": 20})
        )
        if card["qualified_name"] == qualified_name
    ]

    assert len(cards) == 2
    declaration = min(cards, key=lambda card: int(card["line"]))
    implementations = context.handle(
        {
            "action": "inspect",
            "target_id": declaration["target_id"],
            "detail": "implementations",
        }
    )
    counterpart_cards = [
        item for item in result_items(implementations) if item.get("item_type") == "counterpart"
    ]
    assert len(counterpart_cards) == 1

    body_response = context.handle(
        {"action": "inspect", "target_id": declaration["target_id"], "detail": "body"}
    )
    body = "".join(str(item["text"]) for item in result_items(body_response))
    assert marker in body


def test_implementations_returns_counterpart_cards_and_bounded_declarations(tmp_path: Path) -> None:
    context, _, run_target = _worker_context(tmp_path)

    response = context.handle(
        {"action": "inspect", "target_id": run_target, "detail": "implementations"}
    )
    items = result_items(response)

    assert any(
        item.get("qualified_name") == "Worker.TWorker.Run"
        and item.get("target_id") != run_target
        for item in items
    )
    assert "procedure TWorker.Run;" in "".join(str(item.get("text", "")) for item in items)


def _overload_context(tmp_path: Path) -> tuple[AgentContext, list[dict[str, object]]]:
    write_source(
        tmp_path / "Overloads.pas",
        """
        unit Overloads;
        interface
        type
          TOverloaded = class
            procedure Run(Value: Integer); overload;
            procedure Run(Value: string); overload;
          end;
        implementation
        procedure TOverloaded.Run(Value: Integer);
        begin
          IntegerBodyMarker;
        end;
        procedure TOverloaded.Run(Value: string);
        begin
          StringBodyMarker;
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = sorted(
        [
            card
            for card in result_items(
                context.handle({"action": "find", "query": "TOverloaded.Run", "max_items": 20})
            )
            if card["qualified_name"] == "Overloads.TOverloaded.Run"
        ],
        key=lambda card: int(card["line"]),
    )
    return context, cards


def test_implementations_match_the_selected_overload_signature(tmp_path: Path) -> None:
    context, cards = _overload_context(tmp_path)
    declaration = cards[0]

    response = context.handle(
        {
            "action": "inspect",
            "target_id": declaration["target_id"],
            "detail": "implementations",
            "max_items": 20,
        }
    )
    counterpart_cards = [
        item for item in result_items(response) if item.get("item_type") == "counterpart"
    ]
    declarations = "".join(
        str(item.get("text", "")) for item in result_items(response)
    )

    assert len(counterpart_cards) == 1
    assert "Value: Integer" in declarations
    assert "Value: string" not in declarations


@pytest.mark.parametrize(
    ("card_index", "expected_marker", "unexpected_marker"),
    [
        (0, "IntegerBodyMarker", "StringBodyMarker"),
        (1, "StringBodyMarker", "IntegerBodyMarker"),
        (2, "IntegerBodyMarker", "StringBodyMarker"),
        (3, "StringBodyMarker", "IntegerBodyMarker"),
    ],
)
def test_body_matches_declaration_and_implementation_overload_signatures(
    tmp_path: Path,
    card_index: int,
    expected_marker: str,
    unexpected_marker: str,
) -> None:
    context, cards = _overload_context(tmp_path)

    response = context.handle(
        {
            "action": "inspect",
            "target_id": cards[card_index]["target_id"],
            "detail": "body",
            "max_items": 20,
        }
    )
    body = "".join(str(item["text"]) for item in result_items(response))

    assert expected_marker in body
    assert unexpected_marker not in body


def test_calling_conventions_and_nested_procedural_parameters_define_signature_identity(
    tmp_path: Path,
) -> None:
    write_source(
        tmp_path / "CallingConventions.pas",
        """
        unit CallingConventions;
        interface
        procedure Execute(
          const Callback: reference to procedure(InputValue: Integer = 5)
        ); cdecl; overload;
        procedure Execute(
          const Callback: reference to procedure(InputValue: Integer = 5)
        ); stdcall; overload;
        implementation
        procedure Execute(
          const Callback: reference to procedure(RenamedValue: Integer)
        ); cdecl;
        begin
          CdeclBodyMarker;
        end;
        procedure Execute(
          const Callback: reference to procedure(OtherName: Integer)
        ); stdcall;
        begin
          StdcallBodyMarker;
        end;
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = sorted(
        [
            card
            for card in result_items(
                context.handle({"action": "find", "query": "Execute", "max_items": 20})
            )
            if card["name"] == "Execute"
        ],
        key=lambda card: int(card["line"]),
    )

    assert len(cards) == 4
    assert len({card["target_id"] for card in cards}) == 4
    for declaration_index, expected, unexpected in (
        (0, "CdeclBodyMarker", "StdcallBodyMarker"),
        (1, "StdcallBodyMarker", "CdeclBodyMarker"),
    ):
        declaration = cards[declaration_index]
        counterparts = context.handle(
            {
                "action": "inspect",
                "target_id": declaration["target_id"],
                "detail": "implementations",
            }
        )
        assert sum(
            item.get("item_type") == "counterpart"
            for item in result_items(counterparts)
        ) == 1
        body_response = context.handle(
            {"action": "inspect", "target_id": declaration["target_id"], "detail": "body"}
        )
        body = "".join(str(item["text"]) for item in result_items(body_response))
        assert expected in body
        assert unexpected not in body


def test_signature_identity_nfc_normalizes_parameter_type_tokens_across_roots(tmp_path: Path) -> None:
    composed = "Caf\u00e9Type"
    decomposed = unicodedata.normalize("NFD", composed)

    def ids(root: Path, parameter_type: str) -> list[str]:
        write_source(
            root / "UnicodeSignature.pas",
            f"""
            unit UnicodeSignature;
            interface
            procedure UseValue(Value: {parameter_type});
            implementation
            procedure UseValue(Value: {parameter_type});
            begin
            end;
            end.
            """,
        )
        context = AgentContext.open(root)
        cards = sorted(
            [
                card
                for card in result_items(
                    context.handle({"action": "find", "query": "UseValue", "max_items": 20})
                )
                if card["name"] == "UseValue"
            ],
            key=lambda card: int(card["line"]),
        )
        return [str(card["target_id"]) for card in cards]

    assert ids(tmp_path / "first", composed) == ids(tmp_path / "second", decomposed)


def test_long_multiline_routine_declarations_preserve_parameters_return_type_and_directives(
    tmp_path: Path,
) -> None:
    parameters = ";\n".join(
        f"  Value{index:02d}: TDictionary<string, TList<Integer>>"
        for index in range(1, 31)
    )
    source = (
        "unit LongDeclarations;\n"
        "interface\n"
        "function Transform(\n"
        f"{parameters}\n"
        "): Integer;\n"
        "  overload;\n"
        "  deprecated 'Use TransformNew';\n"
        "implementation\n"
        "function Transform(\n"
        f"{parameters}\n"
        "): Integer;\n"
        "begin\n"
        "  Result := 1;\n"
        "end;\n"
        "end.\n"
    )
    (tmp_path / "LongDeclarations.pas").write_text(source, encoding="utf-8")
    context = AgentContext.open(tmp_path)
    cards = [
        card
        for card in result_items(
            context.handle({"action": "find", "query": "Transform", "max_items": 20})
        )
        if card["name"] == "Transform"
    ]
    declaration_target = min(cards, key=lambda card: int(card["line"]))["target_id"]

    declaration = context.handle(
        {
            "action": "inspect",
            "target_id": declaration_target,
            "detail": "declaration",
            "max_chars": 40000,
        }
    )
    narrow_context = context.handle(
        {
            "action": "inspect",
            "target_id": declaration_target,
            "detail": "context",
            "max_chars": 40000,
        }
    )
    implementations = context.handle(
        {
            "action": "inspect",
            "target_id": declaration_target,
            "detail": "implementations",
            "max_chars": 40000,
        }
    )

    declaration_text = "".join(str(item.get("text", "")) for item in result_items(declaration))
    context_text = "".join(str(item.get("text", "")) for item in result_items(narrow_context))
    counterpart_text = "".join(
        str(item.get("text", "")) for item in result_items(implementations)
    )
    for text in (declaration_text, context_text):
        assert "Value01: TDictionary<string, TList<Integer>>;" in text
        assert "Value30: TDictionary<string, TList<Integer>>" in text
        assert "): Integer;" in text
        assert "overload;" in text
        assert "deprecated 'Use TransformNew';" in text
    assert "Value01: TDictionary<string, TList<Integer>>;" in counterpart_text
    assert "Value30: TDictionary<string, TList<Integer>>" in counterpart_text
    assert "): Integer;" in counterpart_text
    assert "begin" not in counterpart_text


def test_forward_structured_types_never_consume_adjacent_real_type_bodies(tmp_path: Path) -> None:
    write_source(
        tmp_path / "ForwardTypes.pas",
        """
        unit ForwardTypes;
        interface
        type
          TForwardClass = class;
          IForwardInterface = interface;
          TRealClass = class
            procedure RealMethod;
          end;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)

    for name, expected in (
        ("TForwardClass", "TForwardClass = class;"),
        ("IForwardInterface", "IForwardInterface = interface;"),
    ):
        target = card_named(context.handle({"action": "find", "query": name}), name)["target_id"]
        declaration = context.handle(
            {"action": "inspect", "target_id": target, "detail": "declaration"}
        )
        declaration_text = "".join(
            str(item["text"]) for item in result_items(declaration)
        )
        assert declaration_text.strip() == expected
        assert "TRealClass" not in declaration_text
        with pytest.raises(AgentProtocolError) as unavailable:
            context.handle({"action": "inspect", "target_id": target, "detail": "body"})
        assert unavailable.value.code == "body_unavailable"

    real_target = card_named(
        context.handle({"action": "find", "query": "TRealClass"}),
        "TRealClass",
    )["target_id"]
    real_body = context.handle(
        {"action": "inspect", "target_id": real_target, "detail": "body"}
    )
    real_text = "".join(str(item["text"]) for item in result_items(real_body))
    assert real_text.startswith("TRealClass = class")
    assert "procedure RealMethod;" in real_text
    assert real_text.rstrip().endswith("end;")


@pytest.mark.parametrize(
    ("name", "required_fragments", "forbidden_fragment"),
    [
        (
            "TNestedProc",
            (
                "reference to function(",
                "Callback: reference to procedure(",
                "InputValue: Integer = 7",
                "): Boolean;",
            ),
            "TRecordArray",
        ),
        (
            "TRecordArray",
            ("array of record", "Value: Integer;", "end;"),
            "TObjectArray",
        ),
        (
            "TObjectArray",
            ("array of object", "procedure Run;", "end;"),
            "implementation",
        ),
    ],
)
def test_multiline_procedural_and_anonymous_array_types_have_complete_isolated_spans(
    tmp_path: Path,
    name: str,
    required_fragments: tuple[str, ...],
    forbidden_fragment: str,
) -> None:
    write_source(
        tmp_path / "ComplexTypes.pas",
        """
        unit ComplexTypes;
        interface
        type
          TNestedProc = reference to function(
            const Callback: reference to procedure(
              InputValue: Integer = 7
            );
            const Items: array of Integer
          ): Boolean;
          TRecordArray = array of record
            Value: Integer;
          end;
          TObjectArray = array of object
            procedure Run;
          end;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    target = card_named(context.handle({"action": "find", "query": name}), name)["target_id"]

    for detail in ("declaration", "body"):
        response = context.handle(
            {"action": "inspect", "target_id": target, "detail": detail, "max_chars": 40000}
        )
        source = "".join(str(item["text"]) for item in result_items(response))
        assert all(fragment in source for fragment in required_fragments)
        assert forbidden_fragment not in source


def test_inspect_uses_focused_target_and_rejects_unknown_or_missing_targets(tmp_path: Path) -> None:
    context, type_target, _ = _worker_context(tmp_path)

    with pytest.raises(AgentProtocolError) as missing:
        context.handle({"action": "inspect"})
    assert missing.value.code == "target_required"

    with pytest.raises(AgentProtocolError) as unknown:
        context.handle({"action": "inspect", "target_id": "target_v2_missing"})
    assert unknown.value.code == "target_not_found"

    context.handle({"action": "focus", "target_id": type_target})
    focused = context.handle({"action": "inspect"})
    assert [item["target_id"] for item in result_items(focused)] == [type_target]


def _write_focus_unit(path: Path, *, include_target: bool, extra: str = "") -> None:
    target = """
      TFocused = class
      end;
    """ if include_target else ""
    write_source(
        path,
        f"""
        unit FocusRevision;
        interface
        type
        {target}
          TOther = class
          end;
        implementation
        {extra}
        end.
        """,
    )


def test_same_project_revision_preserves_a_still_valid_stable_target_focus(tmp_path: Path) -> None:
    source_path = tmp_path / "FocusRevision.pas"
    _write_focus_unit(source_path, include_target=True)
    context = AgentContext.open(tmp_path)
    target = card_named(
        context.handle({"action": "find", "query": "TFocused"}),
        "TFocused",
    )
    context.handle({"action": "focus", "target_id": target["target_id"]})

    _write_focus_unit(source_path, include_target=True, extra="const UnrelatedEdit = 1;")
    rebuilt = context.handle({"action": "find", "query": "TOther"})

    assert rebuilt.focus == Focus(
        project_id=context.workspace.active_project_id,
        unit_id=str(target["unit_id"]),
        target_id=str(target["target_id"]),
    )
    focused_inspect = context.handle({"action": "inspect"})
    assert result_items(focused_inspect)[0]["target_id"] == target["target_id"]


def test_same_project_revision_clears_focus_when_the_target_disappears(tmp_path: Path) -> None:
    source_path = tmp_path / "FocusRevision.pas"
    _write_focus_unit(source_path, include_target=True)
    context = AgentContext.open(tmp_path)
    target = card_named(
        context.handle({"action": "find", "query": "TFocused"}),
        "TFocused",
    )
    context.handle({"action": "focus", "target_id": target["target_id"]})

    _write_focus_unit(source_path, include_target=False)
    rebuilt = context.handle({"action": "find", "query": "TOther"})

    assert rebuilt.focus == Focus(project_id=context.workspace.active_project_id)
    with pytest.raises(AgentProtocolError) as missing:
        context.handle({"action": "inspect"})
    assert missing.value.code == "target_required"


def _write_focus_overloads(path: Path, *, include_boolean: bool) -> None:
    boolean_declaration = "procedure Run(Value: Boolean); overload;" if include_boolean else ""
    boolean_implementation = """
        procedure TFocusedOverloads.Run(Value: Boolean);
        begin
          BooleanBodyMarker;
        end;
    """ if include_boolean else ""
    write_source(
        path,
        f"""
        unit FocusOverloads;
        interface
        type
          TFocusedOverloads = class
            procedure Run(Value: Integer); overload;
            {boolean_declaration}
            procedure Run(Value: string); overload;
          end;
        implementation
        procedure TFocusedOverloads.Run(Value: Integer);
        begin
          IntegerBodyMarker;
        end;
        {boolean_implementation}
        procedure TFocusedOverloads.Run(Value: string);
        begin
          StringBodyMarker;
        end;
        end.
        """,
    )


def test_inserting_an_overload_preserves_existing_signature_target_focus_and_body(tmp_path: Path) -> None:
    source_path = tmp_path / "FocusOverloads.pas"
    _write_focus_overloads(source_path, include_boolean=False)
    context = AgentContext.open(tmp_path)
    initial_cards = sorted(
        [
            card
            for card in result_items(
                context.handle({"action": "find", "query": "TFocusedOverloads.Run", "max_items": 20})
            )
            if card["qualified_name"] == "FocusOverloads.TFocusedOverloads.Run"
        ],
        key=lambda card: int(card["line"]),
    )
    string_declaration = initial_cards[1]
    context.handle({"action": "focus", "target_id": string_declaration["target_id"]})

    _write_focus_overloads(source_path, include_boolean=True)
    refreshed = context.handle(
        {"action": "find", "query": "TFocusedOverloads.Run", "max_items": 20}
    )

    assert refreshed.focus.target_id == string_declaration["target_id"]
    assert any(
        card.get("target_id") == string_declaration["target_id"]
        for card in result_items(refreshed)
    )
    focused_body = context.handle({"action": "inspect", "detail": "body"})
    body = "".join(str(item["text"]) for item in result_items(focused_body))
    assert "StringBodyMarker" in body
    assert "BooleanBodyMarker" not in body


@pytest.mark.parametrize("close_early", [False, True])
def test_branch_dependent_body_uses_cached_parser_span_and_never_reaches_next_routine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_early: bool,
) -> None:
    source = (
        "unit Branches;\n"
        "interface\n"
        "procedure Victim;\n"
        "procedure AfterVictim;\n"
        "implementation\n"
        "procedure Victim;\n"
        "begin\n"
        "{$IFDEF CLOSE_EARLY}\n"
        "  EarlyBodyMarker;\n"
        "end;\n"
        "{$ELSE}\n"
        "  LateBodyMarker;\n"
        "end;\n"
        "{$ENDIF}\n"
        "procedure AfterVictim;\n"
        "begin\n"
        "  AfterVictimMarker;\n"
        "end;\n"
        "end.\n"
    )
    (tmp_path / "Branches.pas").write_text(source, encoding="utf-8")
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Branches in 'Branches.pas';
        begin
        end.
        """,
    )
    define = "<DCC_Define>CLOSE_EARLY</DCC_Define>" if close_early else ""
    write_source(
        tmp_path / "Main.dproj",
        f"""
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            {define}
          </PropertyGroup>
        </Project>
        """,
    )
    context = AgentContext.open(tmp_path)
    cards = [
        card
        for card in result_items(
            context.handle({"action": "find", "query": "Victim", "max_items": 20})
        )
        if card["name"] == "Victim"
    ]
    declaration_target = min(cards, key=lambda card: int(card["line"]))["target_id"]
    parser_calls = 0
    real_parser = agent_context_module.DelphiParser

    class RecordingParser:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._delegate = real_parser(*args, **kwargs)

        def parse(self, *args: object, **kwargs: object):
            nonlocal parser_calls
            parser_calls += 1
            return self._delegate.parse(*args, **kwargs)

    monkeypatch.setattr(agent_context_module, "DelphiParser", RecordingParser)
    request = {
        "action": "inspect",
        "target_id": declaration_target,
        "detail": "body",
        "max_chars": 40000,
    }

    first = context.handle(request)
    second = context.handle(request)
    body = "".join(str(item["text"]) for item in result_items(first))

    assert parser_calls == 1
    assert result_items(second) == result_items(first)
    assert "{$IFDEF CLOSE_EARLY}" in body
    assert "procedure AfterVictim;" not in body
    assert "AfterVictimMarker" not in body
    assert body.startswith("procedure Victim;")
    assert body.rstrip().endswith("end;")
    assert result_items(first)[-1]["end_line"] == (10 if close_early else 13)
    if close_early:
        assert "LateBodyMarker" not in body
    else:
        assert "LateBodyMarker" in body


def _collect_pages(
    context: AgentContext,
    request: dict[str, object],
) -> tuple[list[dict[str, object]], list[AgentResponse]]:
    items: list[dict[str, object]] = []
    responses: list[AgentResponse] = []
    cursor = ""
    while True:
        page_request = dict(request)
        if cursor:
            page_request["cursor"] = cursor
        response = context.handle(page_request)
        responses.append(response)
        items.extend(result_items(response))
        cursor = response.page.next_cursor
        if not cursor:
            return items, responses


def test_body_paginates_and_reconstructs_a_source_with_more_than_100k_lines(tmp_path: Path) -> None:
    source = (
        "unit Huge;\n"
        "interface\n"
        "procedure Big;\n"
        "implementation\n"
        "procedure Big;\n"
        "begin\n"
        + ("\n" * 100_005)
        + "end;\n"
        "end.\n"
    )
    assert len(source.splitlines()) > 100_000
    (tmp_path / "Huge.pas").write_text(source, encoding="utf-8")
    context = AgentContext.open(tmp_path)
    declaration = min(
        (
            card
            for card in result_items(context.handle({"action": "find", "query": "Big", "max_items": 20}))
            if card["name"] == "Big"
        ),
        key=lambda card: int(card["line"]),
    )

    items, responses = _collect_pages(
        context,
        {
            "action": "inspect",
            "target_id": declaration["target_id"],
            "detail": "body",
            "max_items": 50,
            "max_chars": 40000,
        },
    )
    body = "".join(str(item["text"]) for item in items)

    assert len(responses) > 1
    assert responses[0].page.truncated
    assert len(items) == responses[0].page.total
    assert body.startswith("procedure Big;\nbegin\n")
    assert body.endswith("end;")
    assert body.count("\n") > 100_000
    assert all(item["path"] == "Huge.pas" for item in items)
    for response in responses:
        assert_budget(response, 40000)


def test_body_chunks_and_reconstructs_a_huge_single_line_without_truncation(tmp_path: Path) -> None:
    statement_line = "  " + ("DoWork;" * 15_000) + "\n"
    assert len(statement_line) > 100_000
    source = (
        "program OneLine;\n"
        "procedure Huge;\n"
        "begin\n"
        + statement_line
        + "end;\n"
        "begin\n"
        "end.\n"
    )
    (tmp_path / "OneLine.dpr").write_text(source, encoding="utf-8")
    context = AgentContext.open(tmp_path)
    target = card_named(context.handle({"action": "find", "query": "Huge"}), "Huge")

    items, _ = _collect_pages(
        context,
        {
            "action": "inspect",
            "target_id": target["target_id"],
            "detail": "body",
            "max_items": 50,
            "max_chars": 40000,
        },
    )
    body = "".join(str(item["text"]) for item in items)

    assert len(items) > 1
    assert body == "procedure Huge;\nbegin\n" + statement_line + "end;"
    assert [item["chunk_index"] for item in items] == list(range(len(items)))
    assert all(item["chunk_count"] == len(items) for item in items)


def test_many_routines_reuse_cached_token_starts_without_rebuilding_full_token_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routines = "".join(
        f"procedure Routine{index:03d};\nbegin\n  Marker{index:03d};\nend;\n"
        for index in range(150)
    )
    source = "program ManyRoutines;\n" + routines + ("\n" * 100_005) + "begin\nend.\n"
    assert len(source.splitlines()) > 100_000
    (tmp_path / "ManyRoutines.dpr").write_text(source, encoding="utf-8")
    real_bisect_left = agent_context_module.bisect_left
    bisect_sequences: list[object] = []

    def recording_bisect_left(sequence: object, *args: object) -> int:
        bisect_sequences.append(sequence)
        assert isinstance(sequence, tuple)
        return real_bisect_left(sequence, *args)

    monkeypatch.setattr(agent_context_module, "bisect_left", recording_bisect_left)
    context = AgentContext.open(tmp_path)

    response = context.handle({"action": "find", "query": "Routine", "max_items": 50})
    first_target = result_items(response)[0]["target_id"]
    context.handle({"action": "inspect", "target_id": first_target, "detail": "body"})

    assert response.page.total == 151  # 150 routines plus the matching program name.
    assert bisect_sequences
    assert len({id(sequence) for sequence in bisect_sequences}) <= 2


def test_cursor_continuation_mismatch_and_stale_revision_are_precise(tmp_path: Path) -> None:
    source_path = tmp_path / "Cursor.pas"
    write_source(
        source_path,
        """
        unit Cursor;
        interface
        const
          Alpha = 1;
          Beta = 2;
          Gamma = 3;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    request = {"action": "find", "query": "", "max_items": 1, "max_chars": 12000}

    first = context.handle(request)
    assert first.page.truncated
    second = context.handle({**request, "cursor": first.page.next_cursor})
    assert result_items(second) != result_items(first)

    with pytest.raises(AgentProtocolError) as mismatch:
        context.handle({**request, "query": "Alpha", "cursor": first.page.next_cursor})
    assert mismatch.value.code == "cursor_mismatch"

    source_path.write_text(source_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(AgentProtocolError) as stale:
        context.handle({**request, "cursor": first.page.next_cursor})
    assert stale.value.code == "stale_cursor"


def test_cursor_fingerprint_rejects_cross_detail_and_cross_target_use(tmp_path: Path) -> None:
    context, type_target, run_target = _worker_context(tmp_path)
    members = context.handle(
        {
            "action": "inspect",
            "target_id": type_target,
            "detail": "members",
            "max_items": 1,
        }
    )
    assert members.page.next_cursor

    with pytest.raises(AgentProtocolError) as detail_mismatch:
        context.handle(
            {
                "action": "inspect",
                "target_id": type_target,
                "detail": "summary",
                "max_items": 1,
                "cursor": members.page.next_cursor,
            }
        )
    assert detail_mismatch.value.code == "cursor_mismatch"

    with pytest.raises(AgentProtocolError) as target_mismatch:
        context.handle(
            {
                "action": "inspect",
                "target_id": run_target,
                "detail": "members",
                "max_items": 1,
                "cursor": members.page.next_cursor,
            }
        )
    assert target_mismatch.value.code == "cursor_mismatch"


def test_open_returns_paths_defines_includes_problems_and_paginates(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses Included in 'src/Included.pas', MissingUnit;
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "src" / "Included.pas",
        """
        unit Included;
        interface
        {$I 'api.inc'}
        implementation
        end.
        """,
    )
    write_source(tmp_path / "includes" / "api.inc", "const ApiValue = 1;")
    write_source(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_IncludePath>includes</DCC_IncludePath>
            <DCC_Define>AGENT_V2</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    context = AgentContext.open(tmp_path)

    items, responses = _collect_pages(
        context,
        {"action": "open", "max_items": 2, "max_chars": 12000},
    )
    item_types = {str(item["item_type"]) for item in items}

    assert len(responses) > 1
    assert {
        "project",
        "unit",
        "include_file",
        "search_path",
        "include_path",
        "define",
        "problem",
    } <= item_types
    assert any(item.get("define") == "AGENT_V2" for item in items)
    assert any(item.get("path") == "includes/api.inc" for item in items)
    assert all(str(item.get("path", "")).find(str(tmp_path)) < 0 for item in items)
    assert sum(response.page.returned for response in responses) == responses[0].page.total


def test_external_units_use_stable_non_absolute_paths_ids_and_source_items(tmp_path: Path) -> None:
    def inspect_bundle(bundle: Path) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
        project_root = bundle / "project"
        write_source(
            project_root / "Main.dpr",
            """
            program Main;
            uses SharedUnit in '../shared/SharedUnit.pas';
            begin
            end.
            """,
        )
        write_source(
            bundle / "shared" / "SharedUnit.pas",
            """
            unit SharedUnit;
            interface
            type
              TExternal = class
              end;
            procedure ExternalWork;
            implementation
            procedure ExternalWork;
            begin
              ExternalBodyMarker;
            end;
            end.
            """,
        )
        context = AgentContext.open(project_root)
        card = card_named(
            context.handle({"action": "find", "query": "TExternal"}),
            "TExternal",
        )
        routine = min(
            (
                item
                for item in result_items(
                    context.handle({"action": "find", "query": "ExternalWork", "max_items": 20})
                )
                if item["name"] == "ExternalWork"
            ),
            key=lambda item: int(item["line"]),
        )
        body = context.handle(
            {"action": "inspect", "target_id": routine["target_id"], "detail": "body"}
        )
        open_items = result_items(context.handle({"action": "open", "max_items": 50}))
        unit_item = next(item for item in open_items if item.get("name") == "SharedUnit")
        return card, unit_item, result_items(body)

    first_card, first_unit, first_source = inspect_bundle(tmp_path / "first")
    second_card, second_unit, second_source = inspect_bundle(tmp_path / "second")

    expected_path = "@external/SharedUnit/SharedUnit.pas"
    for card, unit, source_items in (
        (first_card, first_unit, first_source),
        (second_card, second_unit, second_source),
    ):
        assert card["path"] == expected_path
        assert unit["path"] == expected_path
        assert card["unit_id"] == unit["unit_id"]
        assert all(item["path"] == expected_path for item in source_items)
        assert all(not Path(str(item["path"])).is_absolute() for item in source_items)
        assert "ExternalBodyMarker" in "".join(str(item["text"]) for item in source_items)
    assert first_card["target_id"] == second_card["target_id"]
    assert first_card["unit_id"] == second_card["unit_id"]
    assert first_unit["unit_id"] == second_unit["unit_id"]


def test_open_and_problems_sanitize_every_external_workspace_path_and_origin(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    project = tmp_path / "outside" / "Main.dpr"
    write_source(
        project,
        """
        program Main;
        uses
          ExternalUnit in 'src/ExternalUnit.pas',
          Broken in 'src/Broken.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "outside" / "src" / "ExternalUnit.pas",
        """
        unit ExternalUnit;
        interface
        {$I 'External.inc'}
        implementation
        end.
        """,
    )
    write_source(tmp_path / "outside" / "src" / "Broken.pas", "unit Broken; invalid syntax")
    write_source(tmp_path / "outside" / "include" / "External.inc", "const ExternalValue = 1;")
    write_source(
        tmp_path / "outside" / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>EXTERNAL_BUILD</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    context = AgentContext.open(root, project_file=project)

    opened = context.handle({"action": "open", "max_items": 50, "max_chars": 40000})
    problems = context.handle({"action": "problems", "max_items": 50, "max_chars": 40000})
    items = result_items(opened)
    serialized = json.dumps(
        [opened.to_mapping(), problems.to_mapping()],
        ensure_ascii=False,
        sort_keys=True,
    )

    project_item = next(item for item in items if item["item_type"] == "project")
    include_item = next(item for item in items if item["item_type"] == "include_file")
    search_item = next(item for item in items if item["item_type"] == "search_path")
    include_path_item = next(item for item in items if item["item_type"] == "include_path")
    define_item = next(item for item in items if item["item_type"] == "define")
    problem_items = [item for item in items if item["item_type"] == "problem"]

    assert project_item["path"] == "@external/project/Main.dpr"
    assert include_item["path"] == "@external/include/External.inc"
    assert search_item["path"] == "@external/search-path/src"
    assert search_item["origins"] == ["@external/origin/Main.dpr"]
    assert include_path_item["path"] == "@external/include-path/include"
    assert include_path_item["origins"] == ["@external/origin/Main.dproj"]
    assert define_item["origins"] == ["@external/origin/Main.dproj"]
    assert problem_items
    assert all(str(item["origin"]).startswith("@external/origin/") for item in problem_items)
    assert any(item.get("path") == "@external/problem/Broken.pas" for item in problem_items)
    assert result_items(problems) == problem_items
    assert str(tmp_path) not in serialized
    assert str(tmp_path.parent) not in serialized
    assert not any(
        Path(value).is_absolute()
        for item in [*items, *result_items(problems)]
        for key, raw in item.items()
        if key in {"path", "origin", "origins"}
        for value in (raw if isinstance(raw, list) else [raw])
        if isinstance(value, str)
    )


def test_problems_are_project_scoped_paginated_items(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Broken.dpr",
        """
        program Broken;
        uses MissingOne, MissingTwo;
        begin
        end.
        """,
    )
    context = AgentContext.open(tmp_path)

    response = context.handle({"action": "problems", "max_items": 1})

    assert response.page.total >= 2
    assert response.page.truncated
    assert result_items(response)[0]["item_type"] == "problem"
    assert result_items(response)[0]["kind"] == "cant_find_file"


def test_problems_requires_project_in_unselected_multi_project_workspace_without_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source(tmp_path / "A.dpr", "program A; begin end.")
    write_source(tmp_path / "B.dpr", "program B; begin end.")
    context = AgentContext.open(tmp_path)
    build_calls = 0

    def forbidden_registry(*args: object, **kwargs: object):
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("problems must not build the symbol registry")

    monkeypatch.setattr(agent_context_module, "_build_registry", forbidden_registry)

    choices = context.handle({"action": "open"})
    assert [item["name"] for item in result_items(choices)] == ["A", "B"]
    assert all(not item["active"] for item in result_items(choices))
    with pytest.raises(AgentProtocolError) as required:
        context.handle({"action": "problems"})
    assert required.value.code == "project_required"
    assert build_calls == 0


def test_small_context_budget_chunks_oversized_cards_without_source_or_loss(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Budget.pas",
        """
        unit Budget;
        interface
        type
          TLongContextBudgetTarget = class
          end;
        implementation
        end.
        """,
    )
    context = AgentContext.open(tmp_path)
    request = {
        "action": "find",
        "query": "TLongContextBudgetTarget",
        "max_items": 50,
        "max_chars": 256,
    }

    items, responses = _collect_pages(context, request)
    serialized_card = "".join(str(item["json"]) for item in items)
    card = json.loads(serialized_card)

    assert card["name"] == "TLongContextBudgetTarget"
    assert all(item["item_type"] == "card_chunk" for item in items)
    assert all("text" not in item and "source" not in item for item in items)
    for response in responses:
        assert_budget(response, 256)


def test_small_context_budget_keeps_source_chunks_typed_and_exact(tmp_path: Path) -> None:
    context, _, run_target = _worker_context(tmp_path)
    expected_response = context.handle(
        {
            "action": "inspect",
            "target_id": run_target,
            "detail": "body",
            "max_items": 50,
            "max_chars": 40000,
        }
    )
    expected = "".join(str(item["text"]) for item in result_items(expected_response))

    items, responses = _collect_pages(
        context,
        {
            "action": "inspect",
            "target_id": run_target,
            "detail": "body",
            "max_items": 50,
            "max_chars": 256,
        },
    )

    assert items
    assert all(item["item_type"] in {"source", "source_chunk"} for item in items)
    assert all(item["item_type"] != "card_chunk" for item in items)
    assert "".join(str(item["text"]) for item in items) == expected
    for response in responses:
        assert_budget(response, 256)


def test_minimum_budget_unicode_single_line_chunks_stay_typed_and_exact(tmp_path: Path) -> None:
    long_line = "  UnicodePayload := '" + ("é漢🚀" * 4_000) + "';\n"
    source = (
        "unit U;\n"
        "interface\n"
        "procedure HugeUnicode;\n"
        "implementation\n"
        "procedure HugeUnicode;\n"
        "begin\n"
        + long_line
        + "end;\n"
        "end.\n"
    )
    assert len(long_line) > 10_000
    (tmp_path / "U.pas").write_text(source, encoding="utf-8")
    context = AgentContext.open(tmp_path)
    target = max(
        (
            item
            for item in result_items(
                context.handle({"action": "find", "query": "HugeUnicode", "max_items": 20})
            )
            if item["name"] == "HugeUnicode"
        ),
        key=lambda item: int(item["line"]),
    )
    expected_response = context.handle(
        {
            "action": "inspect",
            "target_id": target["target_id"],
            "detail": "body",
            "max_items": 50,
            "max_chars": 40_000,
        }
    )
    expected = "".join(str(item["text"]) for item in result_items(expected_response))

    items, responses = _collect_pages(
        context,
        {
            "action": "inspect",
            "target_id": target["target_id"],
            "detail": "body",
            "max_items": 50,
            "max_chars": 256,
        },
    )

    assert items
    assert all(item["item_type"] in {"source", "source_chunk"} for item in items)
    assert max(int(item["end_col"]) for item in items) >= 10_000
    assert "".join(str(item["text"]) for item in items).encode("utf-8") == expected.encode("utf-8")
    assert all(
        len(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + 2 <= 256
        for item in items
    )
    for response in responses:
        assert_budget(response, 256)


def test_unknown_project_and_malformed_requests_keep_protocol_errors(tmp_path: Path) -> None:
    write_source(tmp_path / "Main.dpr", "program Main; begin end.")
    context = AgentContext.open(tmp_path)

    with pytest.raises(AgentProtocolError) as project:
        context.handle({"action": "open", "project_id": "project_v2_missing"})
    assert project.value.code == "project_not_found"

    with pytest.raises(AgentProtocolError) as malformed:
        context.handle({"action": "find", "unknown": True})
    assert malformed.value.code == "unknown_field"
