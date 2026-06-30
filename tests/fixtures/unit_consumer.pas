unit UnitConsumer;

interface

uses
  UnitMath;

procedure UseAdd;

implementation

procedure UseAdd;
var
  Value: Integer;
begin
  Value := Add(1, 2);
end;

end.
