from __future__ import annotations

from delphi_lsp.parser import DelphiParser
from delphi_lsp.parser_backend import ParserMode
from delphi_lsp.semantic import ReferenceKind, SymbolKind


SOURCE = """
unit SemanticFast;

interface

type
  TWorker = class(TObject)
    procedure Run(Value: Integer);
  end;

procedure Touch(Value: Integer);

implementation

procedure Touch(Value: Integer);
begin
end;

procedure TWorker.Run(Value: Integer);
var
  Local: Integer;
begin
  Local := Value;
  if Local > 0 then
    Touch(Local);
end;

end.
"""


def test_tolerant_delphiast_backend_builds_semantic_symbols() -> None:
    result = DelphiParser(mode=ParserMode.TOLERANT).parse(
        SOURCE,
        "SemanticFast.pas",
        build_semantic=True,
    )

    assert result.semantic is not None
    worker = result.semantic.index.lookup("TWorker")[0]
    assert worker.kind is SymbolKind.CLASS
    assert result.semantic.index.lookup("Touch")[0].kind is SymbolKind.PROCEDURE
    assert worker.member_scope is not None
    assert worker.member_scope.lookup_local("Run")


def test_tolerant_delphiast_backend_distinguishes_calls_from_values() -> None:
    result = DelphiParser(mode=ParserMode.TOLERANT).parse(
        SOURCE,
        "SemanticFast.pas",
        build_semantic=True,
    )

    assert result.semantic is not None
    calls = [
        reference
        for reference in result.semantic.references
        if reference.kind is ReferenceKind.CALL
    ]
    values = [
        reference
        for reference in result.semantic.references
        if reference.kind is ReferenceKind.VALUE
    ]
    assert [reference.name for reference in calls] == ["Touch"]
    assert "Local" in {reference.name for reference in values}
    assert "Value" in {reference.name for reference in values}
