unit UnitAttributes;

interface

type
  [MyAttr]
  TBox<T: class, constructor> = class
  private
    [FieldAttr]
    FValue: T;
  public
    [MethodAttr('x')]
    procedure SetValue(const V: T);
    [PropAttr]
    property Value: T read FValue write FValue;
  end;

implementation

procedure TBox.SetValue(const V: T);
begin
  FValue := V;
end;

end.
