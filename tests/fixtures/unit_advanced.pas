unit UnitAdvanced;

interface

type
  TProcRef = reference to procedure;

  IFoo = interface
    procedure DoThing;
  end;

  TImpl = class(TInterfacedObject, IFoo)
  private
    FOnChange: TProcRef;
    procedure IFoo.DoThing = DoThing;
    procedure DoThing;
    procedure AddHandler;
    procedure RemoveHandler;
    function IsStored: Boolean;
  public
    [unsafe]
    procedure Log<T>(const Args: array of const); external 'user32.dll' name 'MessageBoxA' delayed;
    property OnChange: TProcRef read FOnChange write FOnChange add AddHandler remove RemoveHandler stored IsStored;
  end;

procedure Test;

implementation

procedure Test;
var
  Callback: TProcRef;
  I: Integer;
begin
  const InlineConst = 1;
  var InlineVar: Integer := 2;
  inline(1/2/3);
  for var J := 0 to 2 do
    I := J;
  for var K in [1..3, 5] do
    I := K;
  Callback := procedure begin end;
end;

procedure TImpl.AddHandler;
begin
end;

procedure TImpl.RemoveHandler;
begin
end;

function TImpl.IsStored: Boolean;
begin
  Result := True;
end;

procedure TImpl.DoThing;
begin
end;

procedure TImpl.Log<T>(const Args: array of const);
begin
end;

end.
