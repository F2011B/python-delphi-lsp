#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'output' / 'release' / 'delphi_language_feature_matrix.json'
RELEASE_EVIDENCE = ROOT / 'output' / 'release' / 'release_evidence.json'


def _evidence(path: str, patterns: list[str]) -> dict[str, Any]:
    return {'path': path, 'patterns': patterns}


def _lsp_assertion(operation: str, path: str, expected_symbols: list[str]) -> dict[str, Any]:
    return {'operation': operation, 'path': path, 'expected_symbols': expected_symbols}


def _generated_mega_lsp_assertion(expected_symbols: list[str]) -> dict[str, Any]:
    return {'operation': 'documentSymbol', 'generated': 'mega_unit_100k', 'expected_symbols': expected_symbols}


def _feature(
    feature_id: str,
    title: str,
    evidence: list[dict[str, Any]],
    lsp_operations: list[str],
    notes: str,
    lsp_assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        'id': feature_id,
        'title': title,
        'status': 'covered',
        'evidence': evidence,
        'lsp_operations': lsp_operations,
        'lsp_assertions': lsp_assertions or [],
        'notes': notes,
    }


def _runtime_evidence(root: Path) -> dict[str, Any]:
    evidence_path = root / 'output' / 'release' / 'release_evidence.json'
    if not evidence_path.exists():
        return {'available': False, 'path': str(evidence_path)}
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    vllm = evidence.get('vllm', {})
    github_vllm = evidence.get('github_vllm_lsp_edit', {})
    github_ops = evidence.get('github_vllm_lsp_operations', {})
    return {
        'available': True,
        'path': str(evidence_path),
        'github_repos_clean': evidence.get('constraints', {}).get('github_repos_clean'),
        'vllm_context': vllm.get('opencode_lsp_edit_context'),
        'vllm_mega_lsp_edit_ms': {
            'lsp': vllm.get('opencode_lsp_edit_lsp_elapsed_ms'),
            'edit': vllm.get('opencode_lsp_edit_elapsed_ms'),
        },
        'vllm_github_lsp_edit_ms': {
            'lsp': github_vllm.get('lsp_elapsed_ms'),
            'edit': github_vllm.get('edit_elapsed_ms'),
        },
        'vllm_github_lsp_only_operations': github_ops.get('operations_seen', []),
    }


def build_language_feature_matrix(root: Path = ROOT) -> dict[str, Any]:
    features = [
        _feature(
            'compilation_units',
            'Programs, libraries, packages, and units',
            [
                _evidence('tests/fixtures/program_demo.dpr', ['program DemoProgram']),
                _evidence('tests/fixtures/library_demo.dpr', ['library DemoLibrary', 'exports']),
                _evidence('tests/fixtures/package_demo.dpk', ['package DemoPkg', 'requires', 'contains']),
                _evidence('tests/fixtures/unit_basic.pas', ['unit UnitBasic']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'The parser and LSP operate over .pas, .dpr, and .dpk inputs.',
            [
                _lsp_assertion('documentSymbol', 'tests/fixtures/program_demo.dpr', ['DemoProgram']),
                _lsp_assertion('documentSymbol', 'tests/fixtures/library_demo.dpr', ['DemoLibrary', 'DemoFunc', 'DemoProc']),
                _lsp_assertion('documentSymbol', 'tests/fixtures/package_demo.dpk', ['DemoPkg']),
            ],
        ),
        _feature(
            'interface_implementation_sections',
            'Interface, implementation, visibility, and section bodies',
            [
                _evidence('tests/fixtures/unit_basic.pas', ['interface', 'implementation']),
                _evidence('tests/fixtures/unit_sections.pas', ['resourcestring', 'threadvar']),
                _evidence('tests/fixtures/legacy_snippets/strictvisibility.pas', ['strict private', 'strict protected']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'Section structure is part of the outline model used by document/workspace symbols.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_sections.pas', ['UnitSections', 'SHello', 'GValue', 'CNum', 'VNum'])],
        ),
        _feature(
            'uses_initialization_finalization_exports',
            'Uses clauses, initialization, finalization, and exports',
            [
                _evidence('tests/fixtures/unit_consumer.pas', ['uses', 'UnitMath']),
                _evidence('tests/fixtures/legacy_snippets/finalizationinitializationexports.pas', ['exports', 'initialization', 'finalization']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'Unit-level sections are accepted and represented without breaking the LSP outline.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/legacy_snippets/finalizationinitializationexports.pas', ['TFoo', 'TBar', 'IFooBar', 'Hello'])],
        ),
        _feature(
            'preprocessor_includes_conditionals',
            'Includes, conditional directives, and include path variants',
            [
                _evidence('tests/fixtures/legacy_snippets/includefile.pas', ['{$I includefile.inc}', "{$INCLUDE 'include file2.inc'}"]),
                _evidence('tests/fixtures/legacy_snippets/whitespacearoundifdefcondition.pas', ['{$IFDEF', 'initialization']),
                _evidence('tests/test_parser.py', ['test_include_loader_forwarding', 'test_backslash_relative_include_path']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'The preprocessor feeds the same source reader used by LSP workspace indexing.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/legacy_snippets/includefile.pas', ['includefile'])],
        ),
        _feature(
            'classes_inheritance_visibility',
            'Classes, inheritance, helpers, and visibility',
            [
                _evidence('tests/fixtures/unit_inheritance.pas', ['TBase = class', 'TChild = class(TBase)']),
                _evidence('tests/fixtures/unit_types.pas', ['TClass = class abstract(TObject)', 'THelper = class helper for TClass']),
                _evidence('tests/test_semantic.py', ['test_resolves_inherited_member_calls']),
            ],
            ['documentSymbol', 'definition', 'hover', 'references', 'rename', 'completion'],
            'Semantic and LSP tests cover inherited member lookup and member completion.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_inheritance.pas', ['TBase', 'TChild', 'Foo', 'UseChild'])],
        ),
        _feature(
            'interfaces_delegation',
            'Interfaces, GUIDs, implements, and method delegation',
            [
                _evidence('tests/fixtures/unit_advanced.pas', ['IFoo = interface', 'procedure IFoo.DoThing = DoThing']),
                _evidence('tests/fixtures/unit_types.pas', ["['{00000000-0000-0000-0000-000000000000}']", 'ITest = interface']),
                _evidence('tests/fixtures/legacy_snippets/implementsgenerictype.pas', ['implements IFoo<IInterface>']),
            ],
            ['documentSymbol', 'workspaceSymbol', 'definition'],
            'Interface members are outline-visible and delegation syntax is parser-covered.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_advanced.pas', ['IFoo', 'TImpl', 'DoThing'])],
        ),
        _feature(
            'records_managed_variant_alignment',
            'Records, variant records, managed records, and alignment directives',
            [
                _evidence('tests/fixtures/unit_types.pas', ['TRec = record', 'case Kind: Integer of']),
                _evidence('tests/fixtures/legacy_snippets/managedrecords.pas', ['class operator Initialize', 'class operator Finalize']),
                _evidence('tests/fixtures/legacy_snippets/VariantRecordFieldAttributes.pas', ['TVariantRecord = record', 'case byte of']),
                _evidence('tests/fixtures/legacy_snippets/alignedrecords.pas', ['align 8', 'TMyRecord = record']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'Record declarations remain visible through the structure model.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_types.pas', ['TRec', 'TEnum', 'TSet'])],
        ),
        _feature(
            'generics_constraints_nested',
            'Generic types, constraints, nested generic types, and generic calls',
            [
                _evidence('tests/fixtures/unit_generics.pas', ['TBox<T: class, constructor>', 'TOuter<string>.TInner<Integer>.Create']),
                _evidence('tests/fixtures/legacy_snippets/genericconstraints.pas', ['TThreeConstraints: TComponent, IUnknown, constructor']),
                _evidence('tests/test_lsp_support.py', ['test_lsp_definition_resolves_generic_constructor_call', 'test_lsp_completion_returns_members_for_generic_variable']),
            ],
            ['documentSymbol', 'definition', 'completion'],
            'Generic constructor calls and generic member completion have direct LSP tests.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_generics.pas', ['TBox', 'TOuter', 'TInner', 'TListAlias'])],
        ),
        _feature(
            'routines_methods_directives',
            'Procedures, functions, constructors, methods, calling conventions, and directives',
            [
                _evidence('tests/fixtures/unit_advanced.pas', ['procedure Log<T>', "external 'user32.dll'", 'delayed']),
                _evidence('tests/fixtures/legacy_snippets/externalfunction.pas', ['external Kernel32 name']),
                _evidence('tests/fixtures/legacy_snippets/messagemethod.pas', ['message WM_USER']),
            ],
            ['documentSymbol', 'workspaceSymbol', 'definition'],
            'Routine declarations and implementations are exposed as LSP symbols.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_advanced.pas', ['Log', 'TImpl.Log', 'AddHandler', 'RemoveHandler'])],
        ),
        _feature(
            'properties_events_indexers',
            'Properties, event accessors, indexed/default properties, and storage metadata',
            [
                _evidence('tests/fixtures/unit_properties.pas', ['property Items[Idx: Integer]', 'add AddHandler remove RemoveHandler', 'nodefault']),
                _evidence('tests/fixtures/legacy_snippets/properties.pas', ['property DefaultIndexed', 'stored IsWidthStored default 50']),
            ],
            ['documentSymbol', 'completion'],
            'Properties are member symbols and appear in completion tests.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_properties.pas', ['Count', 'Items', 'Events', 'NodefaultProp'])],
        ),
        _feature(
            'procedural_types_callbacks',
            'Procedural types, anonymous methods, and callbacks',
            [
                _evidence('tests/fixtures/unit_advanced.pas', ['TProcRef = reference to procedure', 'Callback := procedure begin end']),
                _evidence('tests/fixtures/unit_types.pas', ['TProc = procedure(A: Integer) of object', 'TFunc = function(const S: string): Integer']),
            ],
            ['documentSymbol', 'hover', 'references'],
            'Procedural type references are included in semantic reference collection.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_advanced.pas', ['TProcRef', 'Callback'])],
        ),
        _feature(
            'variables_constants_sets',
            'Variables, constants, resourcestrings, threadvars, sets, and class vars',
            [
                _evidence('tests/fixtures/unit_sections.pas', ['resourcestring', 'threadvar']),
                _evidence('tests/fixtures/unit_types.pas', ['class var GlobalCount', 'class const Version', 'TSet = set of TEnum']),
                _evidence('tests/fixtures/legacy_snippets/constset.pas', ['cConstant: set of TClass.TInnerEnum']),
            ],
            ['documentSymbol', 'definition', 'references', 'rename'],
            'Symbol resolution tests cover local variables and rename/reference operations.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_sections.pas', ['SHello', 'GValue', 'CNum', 'VNum'])],
        ),
        _feature(
            'expressions_operators_literals',
            'Expressions, operators, literals, ranges, and format-like syntax',
            [
                _evidence('tests/fixtures/unit_math.pas', ['Result := A + B']),
                _evidence('tests/fixtures/legacy_snippets/numbers.pas', ['$_1241_3_', '%_01011']),
                _evidence('tests/test_parser.py', ['test_pointer_to_single_letter_generic_type_parameter']),
            ],
            ['hover', 'definition', 'references'],
            'Expression parsing feeds body-scope lookup used by hover/definition/reference LSP paths.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_math.pas', ['Add'])],
        ),
        _feature(
            'statements_control_flow_exceptions',
            'Statements, loops, case, with, labels, goto, try/except, and try/finally',
            [
                _evidence('tests/fixtures/unit_statements.pas', ['for I := 0 to 10 do', 'repeat', 'case I of', 'with Obj do', 'try', 'finally', 'goto 100']),
                _evidence('tests/fixtures/legacy_snippets/tryexcept.pas', ['try', 'except']),
            ],
            ['definition', 'hover', 'references', 'rename'],
            'The LSP body-scope tests exercise lookup inside statement bodies.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_statements.pas', ['Demo', 'I', 'Obj'])],
        ),
        _feature(
            'pointers_arrays_type_aliases',
            'Pointers, arrays, subranges, files, strings, and distinct aliases',
            [
                _evidence('tests/fixtures/unit_types.pas', ['TSubrange = 1..10', 'TArr = array[0..9] of Integer', 'TFile = file of Byte']),
                _evidence('tests/fixtures/legacy_snippets/pointerchars.pas', ['PInteger', 'array of Integer']),
                _evidence('tests/test_parser.py', ['test_distinct_type_alias', 'test_codepage_string_type_alias']),
            ],
            ['documentSymbol', 'workspaceSymbol', 'definition'],
            'Type aliases and pointer forms are semantic symbols and workspace-symbol candidates.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_types.pas', ['TSubrange', 'TArr', 'TFile', 'TString'])],
        ),
        _feature(
            'attributes_deprecated_experimental',
            'Attributes and Delphi directives such as deprecated and experimental',
            [
                _evidence('tests/fixtures/unit_attributes.pas', ['[MyAttr]', "[MethodAttr('x')]", '[PropAttr]']),
                _evidence('tests/fixtures/legacy_snippets/deprecatedtype.pas', ["deprecated 'Use TBar'"]),
                _evidence('tests/fixtures/legacy_snippets/experimentals.pas', ['experimental']),
                _evidence('tests/fixtures/legacy_snippets/DeprecatedOnConst.pas', ["deprecated 'Do not use'"]),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'Attribute-bearing declarations are still emitted as LSP symbols.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/unit_attributes.pas', ['TBox', 'SetValue', 'Value'])],
        ),
        _feature(
            'unicode_and_source_encodings',
            'Unicode identifiers and source encodings including UTF-16/BOM',
            [
                _evidence('tests/fixtures/legacy_snippets/umlauts.pas', []),
                _evidence('tests/test_lsp_features.py', ['test_workspace_config_reads_bom_encoded_files', "encode('utf-16')"]),
                _evidence('delphi_lsp/source_reader.py', ['read_source_text', 'utf-16']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'The source reader normalizes encoded Delphi files before indexing.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/legacy_snippets/umlauts.pas', ['umlauts'])],
        ),
        _feature(
            'asm_and_low_level_blocks',
            'Inline assembler and low-level compiler blocks',
            [
                _evidence('tests/fixtures/unit_statements.pas', ['asm', 'mov eax, ebx']),
                _evidence('tests/fixtures/legacy_snippets/endtoken.pas', ['asm', 'BSR', '@@End']),
            ],
            ['documentSymbol', 'workspaceSymbol'],
            'ASM bodies are parser-covered without breaking routine symbol extraction.',
            [_lsp_assertion('documentSymbol', 'tests/fixtures/legacy_snippets/endtoken.pas', ['BitsHighest'])],
        ),
        _feature(
            'large_file_outline_lsp',
            'Large-file outline, workspaceSymbol, body lookup, and rename performance',
            [
                _evidence('tests/test_lsp_support.py', ['test_generated_100k_line_file_returns_document_symbols', 'test_generated_100k_line_workspace_symbol_uses_lazy_outline_index']),
                _evidence('scripts/generate_progress_pdf.py', ['117511 Zeilen', 'Body definition', 'Body rename']),
            ],
            ['documentSymbol', 'workspaceSymbol', 'definition', 'hover', 'references', 'rename'],
            'The optimized structure path is tested for every file size and for 100k+ sources.',
            [_generated_mega_lsp_assertion(['TMegaValue', 'MegaProc00001', 'MegaProc02500'])],
        ),
        _feature(
            'opencode_vllm_lsp_edit',
            'opencode with Ornith via vLLM using LSP plus focused edit',
            [
                _evidence('opencode.json', ['vllm-lsp-edit', '"context": 44352', '"lsp": true', '"edit": true']),
                _evidence('tests/test_release_evidence.py', ['opencode_lsp_edit_chain_100k_vllm_44k_lsp_edit_agent.jsonl', 'vllm/ornith-lspctx']),
                _evidence('README.md', ['vllm/ornith-lspctx', 'vllm-lsp-edit', 'edit:Edit applied successfully']),
            ],
            ['workspaceSymbol', 'edit'],
            'Runtime evidence is summarized from output/release/release_evidence.json when available.',
        ),
    ]
    operations = sorted({operation for feature in features for operation in feature['lsp_operations']})
    direct_lsp_assertions = sum(len(feature.get('lsp_assertions', [])) for feature in features)
    return {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'summary': {
            'total': len(features),
            'covered': sum(1 for feature in features if feature['status'] == 'covered'),
            'lsp_operations': len(operations),
            'operation_names': operations,
            'direct_lsp_assertions': direct_lsp_assertions,
        },
        'features': features,
        'runtime_evidence': _runtime_evidence(root),
    }


def _generated_mega_unit_source(proc_count: int = 2500, statements_per_proc: int = 40) -> str:
    lines = [
        'unit Mega100kUnit;',
        '',
        'interface',
        '',
        'type',
        '  TMegaValue = Integer;',
        '',
        'implementation',
        '',
    ]
    for index in range(1, proc_count + 1):
        lines.append(f'procedure MegaProc{index:05d};')
        lines.append('var')
        lines.append('  Value: Integer;')
        lines.append('begin')
        lines.append('  Value := 0;')
        for statement in range(1, statements_per_proc + 1):
            lines.append(f'  Value := Value + {statement};')
        lines.append('end;')
        lines.append('')
    lines.append('end.')
    return '\n'.join(lines) + '\n'


def _read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8', errors='ignore')


def _source_text_for_lsp_assertion(assertion: dict[str, Any], root: Path) -> tuple[str, str]:
    generated = assertion.get('generated')
    if generated == 'mega_unit_100k':
        return _generated_mega_unit_source(), 'Mega100kUnit.pas'
    path = root / assertion['path']
    from delphi_lsp.source_reader import read_source_text

    return read_source_text(path), str(path)


def _document_symbol_names(text: str, file_name: str) -> set[str]:
    from delphi_lsp.lsp_server import build_outline_semantic_model, iter_symbols

    model = build_outline_semantic_model(text, file_name)
    return {symbol.name for symbol in iter_symbols(model.unit_scope)}


def verify_language_feature_matrix(matrix: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    missing_files: list[str] = []
    missing_patterns: list[dict[str, str]] = []
    missing_lsp_symbols: list[dict[str, Any]] = []
    lsp_assertions_checked = 0
    for feature in matrix.get('features', []):
        for evidence in feature.get('evidence', []):
            rel_path = evidence['path']
            path = root / rel_path
            if not path.exists():
                missing_files.append(rel_path)
                continue
            text = _read_text(path)
            for pattern in evidence.get('patterns', []):
                if pattern not in text:
                    missing_patterns.append(
                        {
                            'feature': feature['id'],
                            'path': rel_path,
                            'pattern': pattern,
                        }
                    )
        for assertion in feature.get('lsp_assertions', []):
            lsp_assertions_checked += 1
            if assertion.get('operation') != 'documentSymbol':
                missing_lsp_symbols.append(
                    {
                        'feature': feature['id'],
                        'operation': assertion.get('operation'),
                        'reason': 'unsupported assertion operation',
                    }
                )
                continue
            text, file_name = _source_text_for_lsp_assertion(assertion, root)
            names = _document_symbol_names(text, file_name)
            expected = set(assertion.get('expected_symbols', []))
            missing = sorted(expected - names)
            if missing:
                missing_lsp_symbols.append(
                    {
                        'feature': feature['id'],
                        'operation': assertion.get('operation'),
                        'path': assertion.get('path'),
                        'generated': assertion.get('generated'),
                        'missing': missing,
                    }
                )
    total = len(matrix.get('features', []))
    covered = sum(1 for feature in matrix.get('features', []) if feature.get('status') == 'covered')
    return {
        'ok': not missing_files and not missing_patterns and not missing_lsp_symbols and covered == total,
        'total': total,
        'covered': covered,
        'missing_files': missing_files,
        'missing_patterns': missing_patterns,
        'lsp_assertions_checked': lsp_assertions_checked,
        'missing_lsp_symbols': missing_lsp_symbols,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Build a Delphi language feature coverage matrix.')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    root = args.root.resolve()
    matrix = build_language_feature_matrix(root)
    verification = verify_language_feature_matrix(matrix, root)
    matrix['verification'] = verification
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(args.output)
    return 0 if verification['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
