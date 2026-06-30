library DemoLibrary;

uses
  SysUtils;

exports
  DemoFunc name 'DemoFunc',
  DemoProc index 1 resident;

function DemoFunc: Integer;
begin
  Result := 42;
end;

procedure DemoProc;
begin
end;

begin
end.
