from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'audit_delphi_language_features.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('audit_delphi_language_features', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_language_feature_matrix_lists_required_delphi_feature_groups() -> None:
    module = _load_module()

    matrix = module.build_language_feature_matrix(ROOT)
    by_id = {entry['id']: entry for entry in matrix['features']}

    required = {
        'compilation_units',
        'interface_implementation_sections',
        'uses_initialization_finalization_exports',
        'preprocessor_includes_conditionals',
        'classes_inheritance_visibility',
        'interfaces_delegation',
        'records_managed_variant_alignment',
        'generics_constraints_nested',
        'routines_methods_directives',
        'properties_events_indexers',
        'procedural_types_callbacks',
        'variables_constants_sets',
        'expressions_operators_literals',
        'statements_control_flow_exceptions',
        'pointers_arrays_type_aliases',
        'attributes_deprecated_experimental',
        'unicode_and_source_encodings',
        'asm_and_low_level_blocks',
        'large_file_outline_lsp',
        'opencode_vllm_lsp_edit',
    }

    assert required.issubset(by_id)
    assert len(by_id) >= len(required)

    for feature_id in required:
        entry = by_id[feature_id]
        assert entry['status'] == 'covered', feature_id
        assert entry['evidence'], feature_id
        assert entry['lsp_operations'], feature_id


def test_language_feature_matrix_verifies_local_evidence_files_and_patterns() -> None:
    module = _load_module()

    matrix = module.build_language_feature_matrix(ROOT)
    result = module.verify_language_feature_matrix(matrix, ROOT)

    assert result['ok'] is True
    assert result['missing_files'] == []
    assert result['missing_patterns'] == []
    assert result['covered'] == result['total']


def test_language_feature_matrix_can_be_serialized_for_release_evidence() -> None:
    module = _load_module()

    matrix = module.build_language_feature_matrix(ROOT)

    assert matrix['schema_version'] == 1
    assert matrix['summary']['total'] == len(matrix['features'])
    assert matrix['summary']['covered'] == len(matrix['features'])
    assert matrix['summary']['lsp_operations'] >= 6


def test_language_feature_matrix_has_executable_lsp_assertions_for_each_feature() -> None:
    module = _load_module()

    matrix = module.build_language_feature_matrix(ROOT)
    missing = [
        entry['id']
        for entry in matrix['features']
        if entry['id'] != 'opencode_vllm_lsp_edit' and not entry.get('lsp_assertions')
    ]

    assert missing == []
    assert matrix['summary']['direct_lsp_assertions'] >= 19

    result = module.verify_language_feature_matrix(matrix, ROOT)
    assert result['lsp_assertions_checked'] >= 19
    assert result['missing_lsp_symbols'] == []
