unit UnitGenerics;

interface

uses
  System.Generics.Collections;

type
  TBox<T: class, constructor> = class
  private
    FValue: T;
  public
    constructor Create;
    function Map<U>(const Func: reference to function(const Item: T): U): U;
  end;

  TOuter<T> = class
  public
    type
      TInner<U> = class
      public
        constructor Create;
      end;
  end;

  TListAlias = TList<Integer>;

procedure UseBox;

implementation

procedure UseBox;
var
  Box: TBox<string>;
  Value: Integer;
begin
  Box := TBox<string>.Create();
  var Inner := TOuter<string>.TInner<Integer>.Create();
  var Nested := TBox<TOuter<string>.TInner<Integer>>.Create();
  var Deep := TBox<TOuter<TBox<string>>.TInner<TBox<Integer>>>.Create();
  Value := 0;
end;

end.
