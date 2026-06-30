unit UnitTypes;

interface

type
  TEnum = (One, Two, Three = 3);
  TSubrange = 1..10;
  TSet = set of TEnum;
  TArr = array[0..9] of Integer;
  TString = string[20];
  TFile = file of Byte;
  TProc = procedure(A: Integer) of object;
  TFunc = function(const S: string): Integer;
  TClass = class abstract(TObject)
  private
    FValue: Integer;
  public
    class var GlobalCount: Integer;
    class const Version = 1;
    class procedure ClassProc; static;
    procedure DoIt; virtual;
    property Value: Integer read FValue write FValue;
  end;

  TRec = record
    X: Integer;
    case Kind: Integer of
      0: (Y: Integer);
      1: (Z: Integer);
  end;

  ITest = interface(IInterface)
    ['{00000000-0000-0000-0000-000000000000}']
    procedure Run;
    property Name: string read GetName;
  end;

  THelper = class helper for TClass
    procedure Help;
  end;

implementation

procedure TClass.ClassProc;
begin
end;

procedure TClass.DoIt;
begin
end;

procedure THelper.Help;
begin
end;

end.
