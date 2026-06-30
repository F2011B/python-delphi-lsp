unit UnitProperties;

interface

type
  TNotifier = class
  private
    FCount: Integer;
    procedure SetCount(Value: Integer);
    function GetCount: Integer;
    procedure AddHandler(const Value: Integer);
    procedure RemoveHandler(const Value: Integer);
    function IsStored: Boolean;
  public
    property Count: Integer read GetCount write SetCount stored IsStored default 0;
    property Items[Idx: Integer]: Integer index 1 read GetCount write SetCount default;
    property Events: Integer read GetCount write SetCount add AddHandler remove RemoveHandler;
    property NodefaultProp: Integer read GetCount write SetCount nodefault;
  end;

implementation

procedure TNotifier.SetCount(Value: Integer);
begin
end;

function TNotifier.GetCount: Integer;
begin
  Result := FCount;
end;

procedure TNotifier.AddHandler(const Value: Integer);
begin
end;

procedure TNotifier.RemoveHandler(const Value: Integer);
begin
end;

function TNotifier.IsStored: Boolean;
begin
  Result := True;
end;

end.
