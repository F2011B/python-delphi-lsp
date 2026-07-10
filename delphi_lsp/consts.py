from __future__ import annotations

from enum import IntEnum


class SyntaxNodeType(IntEnum):
    ntUnknown = 0
    ntAbsolute = 1
    ntAdd = 2
    ntAddr = 3
    ntAlignmentParam = 4
    ntAnd = 5
    ntAnonymousMethod = 6
    ntArguments = 7
    ntAs = 8
    ntAssign = 9
    ntAt = 10
    ntAttribute = 11
    ntAttributes = 12
    ntBounds = 13
    ntCall = 14
    ntCase = 15
    ntCaseElse = 16
    ntCaseLabel = 17
    ntCaseLabels = 18
    ntCaseSelector = 19
    ntClassConstraint = 20
    ntConstant = 21
    ntConstants = 22
    ntConstraints = 23
    ntConstructorConstraint = 24
    ntContains = 25
    ntDefault = 26
    ntDeref = 27
    ntDimension = 28
    ntDiv = 29
    ntDot = 30
    ntDownTo = 31
    ntElement = 32
    ntElse = 33
    ntEmptyStatement = 34
    ntEnum = 35
    ntEqual = 36
    ntExcept = 37
    ntExceptionHandler = 38
    ntExports = 39
    ntExpression = 40
    ntExpressions = 41
    ntExternal = 42
    ntFDiv = 43
    ntField = 44
    ntFields = 45
    ntFinalization = 46
    ntFinally = 47
    ntFor = 48
    ntFrom = 49
    ntGeneric = 50
    ntGoto = 51
    ntGreater = 52
    ntGreaterEqual = 53
    ntGuid = 54
    ntHelper = 55
    ntIdentifier = 56
    ntIf = 57
    ntImplementation = 58
    ntImplements = 59
    ntIn = 60
    ntIndex = 61
    ntIndexed = 62
    ntInherited = 63
    ntInitialization = 64
    ntInterface = 65
    ntIs = 66
    ntLabel = 67
    ntLHS = 68
    ntLiteral = 69
    ntLower = 70
    ntLowerEqual = 71
    ntMessage = 72
    ntMethod = 73
    ntMod = 74
    ntMul = 75
    ntName = 76
    ntNamedArgument = 77
    ntNotEqual = 78
    ntNot = 79
    ntOr = 80
    ntPackage = 81
    ntParameter = 82
    ntParameters = 83
    ntPath = 84
    ntPositionalArgument = 85
    ntProtected = 86
    ntPrivate = 87
    ntProperty = 88
    ntPublic = 89
    ntPublished = 90
    ntRaise = 91
    ntRead = 92
    ntRecordConstraint = 93
    ntRepeat = 94
    ntRequires = 95
    ntResolutionClause = 96
    ntResourceString = 97
    ntReturnType = 98
    ntRHS = 99
    ntRoundClose = 100
    ntRoundOpen = 101
    ntSet = 102
    ntShl = 103
    ntShr = 104
    ntStatement = 105
    ntStatements = 106
    ntStrictPrivate = 107
    ntStrictProtected = 108
    ntSub = 109
    ntSubrange = 110
    ntThen = 111
    ntTo = 112
    ntTry = 113
    ntType = 114
    ntTypeArgs = 115
    ntTypeDecl = 116
    ntTypeParam = 117
    ntTypeParams = 118
    ntTypeSection = 119
    ntValue = 120
    ntVariable = 121
    ntVariables = 122
    ntXor = 123
    ntUnaryMinus = 124
    ntUnit = 125
    ntUses = 126
    ntWhile = 127
    ntWith = 128
    ntWrite = 129
    ntAnsiComment = 130
    ntBorComment = 131
    ntSlashesComment = 132
    ntIsNot = 133
    ntNotIn = 134
    ntInterfaceConstraint = 135
    ntUnmanagedConstraint = 136


class AttributeName(IntEnum):
    anType = 0
    anClass = 1
    anForwarded = 2
    anKind = 3
    anName = 4
    anVisibility = 5
    anCallingConvention = 6
    anPath = 7
    anMethodBinding = 8
    anReintroduce = 9
    anOverload = 10
    anAbstract = 11
    anInline = 12
    anAlign = 13
    anExternal = 14
    anDeprecated = 15
    anStatic = 16
    anFinal = 17
    anSealed = 18
    anAssembler = 19
    anUnsafe = 20
    anExport = 21
    anFar = 22
    anNear = 23
    anGenericArgs = 24
    anNoReturn = 25
    anVarArgs = 26


SYNTAX_NODE_NAMES = (
    'unknown',
    'absolute',
    'add',
    'addr',
    'alignmentparam',
    'and',
    'anonymousmethod',
    'arguments',
    'as',
    'assign',
    'at',
    'attribute',
    'attributes',
    'bounds',
    'call',
    'case',
    'caseelse',
    'caselabel',
    'caselabels',
    'caseselector',
    'classconstraint',
    'constant',
    'constants',
    'constraints',
    'constructorconstraint',
    'contains',
    'default',
    'deref',
    'dimension',
    'div',
    'dot',
    'downto',
    'element',
    'else',
    'emptystatement',
    'enum',
    'equal',
    'except',
    'exceptionhandler',
    'exports',
    'expression',
    'expressions',
    'external',
    'fdiv',
    'field',
    'fields',
    'finalization',
    'finally',
    'for',
    'from',
    'generic',
    'goto',
    'greater',
    'greaterequal',
    'guid',
    'helper',
    'identifier',
    'if',
    'implementation',
    'implements',
    'in',
    'index',
    'indexed',
    'inherited',
    'initialization',
    'interface',
    'is',
    'label',
    'lhs',
    'literal',
    'lower',
    'lowerequal',
    'message',
    'method',
    'mod',
    'mul',
    'name',
    'namedargument',
    'notequal',
    'not',
    'or',
    'package',
    'parameter',
    'parameters',
    'path',
    'positionalargument',
    'protected',
    'private',
    'property',
    'public',
    'published',
    'raise',
    'read',
    'recordconstraint',
    'repeat',
    'requires',
    'resolutionclause',
    'resourcestring',
    'returntype',
    'rhs',
    'roundclose',
    'roundopen',
    'set',
    'shl',
    'shr',
    'statement',
    'statements',
    'strictprivate',
    'strictprotected',
    'sub',
    'subrange',
    'then',
    'to',
    'try',
    'type',
    'typeargs',
    'typedecl',
    'typeparam',
    'typeparams',
    'typesection',
    'value',
    'variable',
    'variables',
    'xor',
    'unaryminus',
    'unit',
    'uses',
    'while',
    'with',
    'write',
    'ansicomment',
    'borlandcomment',
    'slashescomment',
    'isnot',
    'notin',
    'interfaceconstraint',
    'unmanagedconstraint',
)


ATTRIBUTE_NAME_STRINGS = (
    'type',
    'class',
    'forwarded',
    'kind',
    'name',
    'visibility',
    'callingconvention',
    'path',
    'methodbinding',
    'reintroduce',
    'overload',
    'abstract',
    'inline',
    'align',
    'external',
    'deprecated',
    'static',
    'final',
    'sealed',
    'assembler',
    'unsafe',
    'export',
    'far',
    'near',
    'genericargs',
    'noreturn',
    'varargs',
)
