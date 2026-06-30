unit UnitUnresolved;

interface

uses
  MissingUnit;

type
  TFoo = class(TMissingBase)
  public
    Value: UnknownType;
  end;

implementation

end.
