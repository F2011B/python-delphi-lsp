from __future__ import annotations

import json
import math

import pytest

from delphi_lsp.metrics import analyze_project, analyze_unit


def test_line_metrics_classify_comments_directives_and_source_exclusively() -> None:
    source = """unit LineSample; // mixed comment
{ block comment
  continued }

interface
{$IFDEF DEBUG}
const Value = 1;
{$ENDIF}
implementation
end.
"""

    analysis = analyze_unit(source, "LineSample.pas")

    assert analysis.lines.total_lines == 10
    assert analysis.lines.source_lines == 5
    assert analysis.lines.blank_lines == 1
    assert analysis.lines.comment_only_lines == 2
    assert analysis.lines.comment_lines == 3
    assert analysis.lines.directive_lines == 2
    assert (
        analysis.lines.source_lines
        + analysis.lines.blank_lines
        + analysis.lines.comment_only_lines
        + analysis.lines.directive_lines
        == analysis.lines.total_lines
    )


def test_unit_metrics_cover_routine_complexity_halstead_and_symbols() -> None:
    source = """unit MetricSample;
interface
procedure Simple;
procedure Score(Value: Integer);
implementation
procedure Simple;
begin
end;
procedure Score(Value: Integer);
begin
  if Value > 0 then
    Value := Value - 1;
  for Value := 1 to 2 do
    Value := Value + 1;
  while Value > 0 do
    Value := Value - 1;
  repeat
    Value := Value + 1;
  until Value > 0;
  case Value of
    1: Value := 2;
    2, 3: Value := 4;
  else
    Value := 0;
  end;
  try
    Value := 1;
  except
    on E: Exception do Value := 2;
  end;
  try
    Value := 3;
  except
    Value := 4;
  end;
end;
end.
"""

    analysis = analyze_unit(source, "MetricSample.pas")

    assert [(item.name, item.value) for item in analysis.cyclomatic.routines] == [
        ("Score", 9),
        ("Simple", 1),
    ]
    assert analysis.cyclomatic.routine_count == 2
    assert analysis.cyclomatic.total == 10
    assert analysis.cyclomatic.maximum == 9
    assert analysis.cyclomatic.average == 5.0
    assert analysis.symbol_counts["procedure"] >= 2
    assert analysis.halstead.length == (
        analysis.halstead.total_operators + analysis.halstead.total_operands
    )
    assert analysis.halstead.vocabulary == (
        analysis.halstead.distinct_operators + analysis.halstead.distinct_operands
    )
    assert analysis.halstead.volume == pytest.approx(
        analysis.halstead.length * math.log2(analysis.halstead.vocabulary)
    )
    assert analysis.halstead.effort == pytest.approx(
        analysis.halstead.difficulty * analysis.halstead.volume
    )
    assert 0.0 <= analysis.maintainability_index <= 100.0


def test_empty_source_has_finite_zero_metrics_and_full_maintainability() -> None:
    analysis = analyze_unit("", "Empty.pas")
    payload = analysis.to_mapping(detail=True)

    assert analysis.lines.total_lines == 0
    assert analysis.cyclomatic.total == 0
    assert analysis.halstead.volume == 0.0
    assert analysis.maintainability_index == 100.0
    assert "NaN" not in json.dumps(payload, allow_nan=False)


def test_project_metrics_compute_coupling_main_sequence_and_loc() -> None:
    project = analyze_project(
        {
            "Main.dpr": "program Main; uses Alpha; begin end.\n",
            "Alpha.pas": (
                "unit Alpha; interface uses Beta, External.Api; "
                "implementation end.\n"
            ),
            "Beta.pas": (
                "unit Beta; interface type IService = interface end; "
                "implementation end.\n"
            ),
        },
        include_sources={"Shared.inc": "const Shared = 1;\n"},
    )

    main = project.unit_by_name("Main")
    alpha = project.unit_by_name("alpha")
    beta = project.unit_by_name("Beta")

    assert main.afferent_coupling == 0
    assert main.efferent_coupling == 1
    assert alpha.afferent_coupling == 1
    assert alpha.efferent_coupling == 2
    assert alpha.internal_dependencies == ("Beta",)
    assert alpha.external_dependencies == ("External.Api",)
    assert alpha.instability == pytest.approx(2 / 3)
    assert beta.afferent_coupling == 1
    assert beta.efferent_coupling == 0
    assert beta.abstractness == 1.0
    assert beta.distance == 0.0
    assert project.total_loc == 3
    assert project.include_loc == 1
    assert project.total_loc_with_includes == 4
    assert project.unit_count == 3
    assert project.dependency_edges == 3


def test_abstractness_counts_abstract_classes_and_interfaces() -> None:
    source = """unit AbstractTypes;
interface
type
  TConcrete = class end;
  TAbstract = class abstract end;
  IService = interface end;
implementation
end.
"""

    analysis = analyze_unit(source, "AbstractTypes.pas")

    assert analysis.class_like_types == 3
    assert analysis.abstract_types == 2
    assert analysis.abstractness == pytest.approx(2 / 3)


def test_project_halstead_is_recomputed_from_combined_token_vocabulary() -> None:
    first = "unit First; interface const Shared = 1; implementation end.\n"
    second = "unit Second; interface const Shared = 2; implementation end.\n"
    project = analyze_project({"First.pas": first, "Second.pas": second})

    assert project.halstead.total_operators == sum(
        unit.halstead.total_operators for unit in project.units
    )
    assert project.halstead.total_operands == sum(
        unit.halstead.total_operands for unit in project.units
    )
    assert project.halstead.distinct_operands < sum(
        unit.halstead.distinct_operands for unit in project.units
    )
    assert 0.0 <= project.maintainability_index <= 100.0
