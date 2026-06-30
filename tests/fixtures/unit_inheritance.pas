unit UnitInheritance;

interface

type
  TBase = class
  public
    procedure Foo;
  end;

  TChild = class(TBase)
  end;

procedure UseChild;

implementation

procedure TBase.Foo;
begin
end;

procedure UseChild;
var
  Child: TChild;
begin
  Child.Foo();
end;

end.
