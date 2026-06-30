unit UnitStatements;

interface

implementation

procedure Demo;
label
  100;
var
  I: Integer;
  Obj: TObject;
begin
  I := 0;
  for I := 0 to 10 do
    Inc(I);
  for I := 10 downto 0 do
    Dec(I);
  for I in [1,2,3] do
    Inc(I);
  while I < 10 do
    Inc(I);
  repeat
    Dec(I);
  until I = 0;
  case I of
    0: I := 1;
    1,2: I := 3;
  else
    I := 4;
  end;
  with Obj do
    I := 5;
  try
    I := 6;
  except
    on E: Exception do
      I := 7;
  end;
  try
    I := 8;
  finally
    I := 9;
  end;
  asm
    mov eax, ebx
  end;
  goto 100;
  100: I := 11;
end;

end.
