unit UnitWith;

interface

type
  TThing = class
  public
    Value: Integer;
    procedure DoIt;
  end;

procedure UseThing;

implementation

procedure TThing.DoIt;
begin
end;

procedure UseThing;
var
  Thing: TThing;
begin
  with Thing do
  begin
    Value := 1;
    DoIt();
  end;
end;

end.
