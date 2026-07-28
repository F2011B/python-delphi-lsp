from __future__ import annotations

from time import perf_counter

from delphi_lsp.consts import AttributeName, SyntaxNodeType
from delphi_lsp.delphiast_parser import DelphiAstParser
from delphi_lsp.nodes import ValuedSyntaxNode


SOURCE = """
unit FastDemo;

interface

uses SysUtils, Generics.Collections;

type
  TMode = (mdOff, mdOn);
  TWorker = class(TObject)
  private
    FCount: Integer;
  public
    procedure Run(const Values: array of Integer);
  end;

const
  DefaultCount = 3;

var
  GlobalCount: Integer;

implementation

procedure TWorker.Run(const Values: array of Integer);
var
  Index: Integer;
begin
  for Index := 0 to DefaultCount do
    if Values[Index] > 0 then
      GlobalCount := GlobalCount + Values[Index];
end;

end.
"""


def walk(node):
    yield node
    for child in node.child_nodes:
        yield from walk(child)


def test_structural_parser_builds_unit_sections_and_dependencies() -> None:
    result = DelphiAstParser(SOURCE, "FastDemo.pas").parse()

    assert result.root.get_attribute(AttributeName.anName) == "FastDemo"
    assert result.root.find_node(SyntaxNodeType.ntInterface) is not None
    assert result.root.find_node(SyntaxNodeType.ntImplementation) is not None
    uses = next(node for node in walk(result.root) if node.typ == SyntaxNodeType.ntUses)
    assert [
        child.get_attribute(AttributeName.anName)
        for child in uses.child_nodes
        if child.typ == SyntaxNodeType.ntUnit
    ] == ["SysUtils", "Generics.Collections"]


def test_structural_parser_builds_declarations_and_type_members() -> None:
    result = DelphiAstParser(SOURCE, "FastDemo.pas").parse()
    nodes = list(walk(result.root))

    declarations = {
        node.get_attribute(AttributeName.anName): node
        for node in nodes
        if node.typ == SyntaxNodeType.ntTypeDecl
    }
    assert set(declarations) >= {"TMode", "TWorker"}
    worker_type = declarations["TWorker"].find_node(SyntaxNodeType.ntType)
    assert worker_type is not None
    assert worker_type.get_attribute(AttributeName.anType) == "class"
    assert any(node.typ == SyntaxNodeType.ntField for node in walk(worker_type))
    assert any(
        node.typ == SyntaxNodeType.ntMethod
        and node.get_attribute(AttributeName.anName) == "Run"
        for node in walk(worker_type)
    )
    assert any(
        isinstance(node, ValuedSyntaxNode) and node.value == "GlobalCount"
        for node in nodes
    )


def test_structural_parser_builds_routines_blocks_and_control_flow() -> None:
    result = DelphiAstParser(SOURCE, "FastDemo.pas").parse()
    nodes = list(walk(result.root))

    implementation_run = next(
        node
        for node in nodes
        if node.typ == SyntaxNodeType.ntMethod
        and node.get_attribute(AttributeName.anName) == "TWorker.Run"
        and node.find_node(SyntaxNodeType.ntStatements) is not None
    )
    parameters = implementation_run.find_node(SyntaxNodeType.ntParameters)
    assert parameters is not None
    assert any(node.typ == SyntaxNodeType.ntParameter for node in parameters.child_nodes)
    assert any(node.typ == SyntaxNodeType.ntFor for node in walk(implementation_run))
    assert any(node.typ == SyntaxNodeType.ntIf for node in walk(implementation_run))
    assert any(node.typ == SyntaxNodeType.ntAssign for node in walk(implementation_run))


def test_tolerant_parser_recovers_and_always_makes_progress() -> None:
    source = "unit Broken; interface type TFoo = class ??? public X: Integer; end implementation begin @@@ end."
    started = perf_counter()

    result = DelphiAstParser(source, "Broken.pas").parse()

    assert perf_counter() - started < 0.5
    assert result.root.get_attribute(AttributeName.anName) == "Broken"
    assert result.problems
    assert all(problem.line >= 1 and problem.column >= 1 for problem in result.problems)


def test_less_than_in_statement_does_not_swallow_following_declarations() -> None:
    source = """
unit LessThanUnit;
interface
implementation

procedure First;
var
  Index: Integer;
begin
  while Index < 10 do
    Index := Index + 1;
end;

procedure AfterComparison;
begin
end;

end.
"""

    result = DelphiAstParser(source, "LessThanUnit.pas").parse()
    routine_names = [
        node.get_attribute(AttributeName.anName)
        for node in walk(result.root)
        if node.typ == SyntaxNodeType.ntMethod
    ]

    assert routine_names == ["First", "AfterComparison"]


def test_metaclass_and_helper_types_preserve_following_declarations() -> None:
    source = """
unit TypeForms;
interface
type
  TBase = class
  end;
  TBaseClass = class of TBase;
  TBaseHelper = class helper for TBase
    procedure Help;
  end;
  TAfter = class
  private
    FValue: Integer;
  end;
implementation
end.
"""

    result = DelphiAstParser(source, "TypeForms.pas").parse()
    declarations = {
        node.get_attribute(AttributeName.anName): node
        for node in walk(result.root)
        if node.typ == SyntaxNodeType.ntTypeDecl
    }

    assert list(declarations) == [
        "TBase",
        "TBaseClass",
        "TBaseHelper",
        "TAfter",
    ]
    class_reference = declarations["TBaseClass"].find_node(SyntaxNodeType.ntType)
    assert class_reference is not None
    assert class_reference.get_attribute(AttributeName.anType) == "classref"
    assert any(
        node.typ == SyntaxNodeType.ntMethod
        and node.get_attribute(AttributeName.anName) == "Help"
        for node in walk(declarations["TBaseHelper"])
    )
    assert any(
        node.typ == SyntaxNodeType.ntField
        and any(
            isinstance(child, ValuedSyntaxNode) and child.value == "FValue"
            for child in walk(node)
        )
        for node in walk(declarations["TAfter"])
    )


def test_half_typed_enum_value_reports_a_problem_without_raising() -> None:
    source = """
unit IncompleteEnum;
interface
type
  TAlign = (alNone = 0, alTop =);
implementation
end.
"""

    result = DelphiAstParser(source, "IncompleteEnum.pas").parse()
    enum_names = [
        child.value
        for node in walk(result.root)
        if node.typ == SyntaxNodeType.ntEnum
        for child in node.child_nodes
        if isinstance(child, ValuedSyntaxNode)
    ]

    assert enum_names == ["alNone", "alTop"]
    assert any(
        "Expected enum value after '='" in problem.message
        for problem in result.problems
    )


def test_nested_routine_and_outer_body_stay_attached_to_outer_routine() -> None:
    source = """
unit NestedRoutine;
interface
implementation

procedure Outer;
var
  Value: Integer;
  procedure Inner;
  begin
    Value := 1;
  end;
begin
  Inner;
end;

end.
"""

    result = DelphiAstParser(source, "NestedRoutine.pas").parse()
    methods = {
        node.get_attribute(AttributeName.anName): node
        for node in walk(result.root)
        if node.typ == SyntaxNodeType.ntMethod
    }
    outer = methods["Outer"]
    inner = methods["Inner"]

    assert inner.parent_node is outer
    assert sum(
        child.typ == SyntaxNodeType.ntStatements
        for child in outer.child_nodes
    ) == 1
    assert inner.find_node(SyntaxNodeType.ntStatements) is not None
