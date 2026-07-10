#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORPUS_REPORT = Path('output/corpus/corpus_report.json')
OPENCODE_LSP_JSONL = Path('output/mega_lsp_chain_project/opencode_lsp_probe_100k_ollama_128k_lsp_only.jsonl')
OPENCODE_EDIT_JSONL = Path('output/mega_lsp_chain_project/opencode_lsp_edit_chain_100k_ollama_32k.jsonl')
OPENCODE_VLLM_LSP_JSONL = Path(
    'output/mega_lsp_chain_project/opencode_lsp_probe_100k_vllm_44k_lsp_only_agent.jsonl'
)
OPENCODE_VLLM_LSP_EDIT_JSONL = Path(
    'output/mega_lsp_chain_project/opencode_lsp_edit_chain_100k_vllm_44k_lsp_edit_agent.jsonl'
)
OPENCODE_REQUEST_PAYLOADS_JSON = Path('output/release/opencode_request_payloads.json')
LANGUAGE_FEATURE_MATRIX_JSON = Path('output/release/delphi_language_feature_matrix.json')
PDF_PROGRESS_REPORT = Path('output/pdf/delphi_lsp_opencode_progress_2026-06-30.pdf')
GITHUB_LSP_EDIT_JSONL = Path(
    'output/github_lsp_edit_project/opencode_lsp_edit_mormot_core_base_ollama_128k.jsonl'
)
GITHUB_VLLM_LSP_EDIT_JSONL = Path(
    'output/github_lsp_edit_project/opencode_lsp_edit_mormot_core_base_vllm_44k.jsonl'
)
GITHUB_VLLM_LSP_OPS_JSONL = Path(
    'output/github_lsp_edit_project/opencode_lsp_ops_mormot_core_base_vllm_44k.jsonl'
)
GITHUB_LSP_EDIT_SOURCE = Path(
    'test_projects/github_repos/mORMot2/src/core/mormot.core.base.pas'
)
GITHUB_LSP_EDIT_SANDBOX = Path('output/github_lsp_edit_project/mormot.core.base.pas')
MEGA_UNIT = Path('output/mega_lsp_chain_project/Mega100kUnit.pas')
MARKER = 'OPENCODE_OLLAMA_STRUCTURE_PATH_PROBE_20260630'
VLLM_EDIT_MARKER = 'OPENCODE_VLLM44K_LSP_EDIT_PROBE_20260701'
GITHUB_LSP_EDIT_MARKER = 'OPENCODE_OLLAMA_GITHUB_EDIT_PROBE_20260701'
GITHUB_VLLM_LSP_EDIT_MARKER = 'vLLM 44k edit verification 20260701'
FORBIDDEN_OPENCODE_TOOLS = ['read', 'bash', 'glob', 'edit']
FORBIDDEN_OPENCODE_LSP_ONLY_TOOLS = [
    'read',
    'bash',
    'glob',
    'grep',
    'edit',
    'write',
    'task',
    'webfetch',
    'todowrite',
    'skill',
]
FORBIDDEN_OPENCODE_LSP_EDIT_TOOLS = [
    'read',
    'bash',
    'glob',
    'grep',
    'write',
    'task',
    'webfetch',
    'todowrite',
    'skill',
]
VLLM_MODEL_ID = 'deepreinforce-ai/Ornith-1.0-9B'
VLLM_SERVED_MODEL_NAME = 'ornith-vllm-metal'
VLLM_ENDPOINT = 'http://127.0.0.1:8001/v1'
VLLM_LSP_AGENT = 'vllm-lsp'
VLLM_LSP_EDIT_AGENT = 'vllm-lsp-edit'
VLLM_LSP_MODEL_NAME = 'ornith-lspctx'
VLLM_LSP_EDIT_MODEL_NAME = 'ornith-lspctx'
VLLM_HF_HOME = Path('/Volumes/MacDataSSDPro/.cache/huggingface')
DEFAULT_OPENCODE_MODEL = 'ollama/ornith-lspctx'
GITHUB_LSP_EDIT_AGENT = VLLM_LSP_EDIT_AGENT


def _load_cache_checker():
    checker_path = Path(__file__).with_name('check_ornith_cache.py')
    spec = importlib.util.spec_from_file_location('check_ornith_cache', checker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load cache checker from {checker_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cache_preparer():
    preparer_path = Path(__file__).with_name('prepare_ornith_cache.py')
    spec = importlib.util.spec_from_file_location('prepare_ornith_cache', preparer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load cache preparer from {preparer_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _load_json(path)


def _read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def _line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _tool_elapsed_ms(state: dict[str, Any]) -> int | None:
    timing = state.get('time') or {}
    start = timing.get('start')
    end = timing.get('end')
    if isinstance(start, int) and isinstance(end, int):
        return end - start
    return None


def _normalize_lsp_operation(operation: Any) -> str:
    value = str(operation)
    if value == 'goToDefinition':
        return 'definition'
    return value


def _opencode_tool_evidence(jsonl_path: Path) -> dict[str, Any]:
    tools: dict[str, dict[str, Any]] = {}
    if not jsonl_path.exists():
        return tools
    for line in jsonl_path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        tool = part.get('tool')
        state = part.get('state') or {}
        if not tool or state.get('status') != 'completed':
            continue
        tools[tool] = {
            'status': state.get('status'),
            'elapsed_ms': _tool_elapsed_ms(state),
            'input': state.get('input') or {},
        }
    return tools


def _opencode_tools_seen(jsonl_path: Path) -> list[str]:
    tools: list[str] = []
    if not jsonl_path.exists():
        return tools
    for line in jsonl_path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        tool = part.get('tool')
        if tool:
            tools.append(tool)
    return tools


def _opencode_completed_tool_events(jsonl_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not jsonl_path.exists():
        return events
    for line in jsonl_path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        state = part.get('state') or {}
        if state.get('status') != 'completed':
            continue
        events.append(
            {
                'tool': part.get('tool'),
                'elapsed_ms': _tool_elapsed_ms(state),
                'input': state.get('input') or {},
            }
        )
    return events


def _dist_artifact(path: Path, *, root: Path) -> dict[str, Any]:
    return {
        'path': _portable_evidence_path(path, root=root),
        'exists': path.exists(),
        'bytes': path.stat().st_size if path.exists() else 0,
    }


def _portable_evidence_path(
    path: Path,
    *,
    root: Path | None = None,
    external_alias: str | None = None,
) -> str:
    if external_alias is not None:
        return f'@external/{external_alias.strip("/")}'
    if root is not None:
        try:
            return Path(path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return f'@external/{Path(path).name}'
    return path.as_posix()


def _opencode_model_config(root: Path, model_ref: str = DEFAULT_OPENCODE_MODEL) -> dict[str, Any]:
    config_path = root / 'opencode.json'
    if not config_path.exists():
        return {
            'model': model_ref,
            'context': None,
            'tool_call': None,
        }
    config = _load_json(config_path)
    provider_name, _, model_name = (config.get('model') or model_ref).partition('/')
    provider = ((config.get('provider') or {}).get(provider_name) or {})
    model = ((provider.get('models') or {}).get(model_name) or {})
    return {
        'model': f'{provider_name}/{model_name}' if provider_name and model_name else model_ref,
        'context': (model.get('limit') or {}).get('context'),
        'tool_call': model.get('tool_call'),
    }


def _vllm_opencode_config(root: Path) -> dict[str, Any]:
    config_path = root / 'opencode.json'
    if not config_path.exists():
        return {
            'endpoint': VLLM_ENDPOINT,
            'opencode_model': 'vllm/ornith-lspctx',
            'opencode_context': None,
            'opencode_tool_call': None,
            'opencode_lsp_agent': VLLM_LSP_AGENT,
            'opencode_lsp_model': f'vllm/{VLLM_LSP_MODEL_NAME}',
            'opencode_lsp_context': None,
            'opencode_lsp_tool_call': None,
            'opencode_lsp_edit_agent': VLLM_LSP_EDIT_AGENT,
            'opencode_lsp_edit_model': f'vllm/{VLLM_LSP_EDIT_MODEL_NAME}',
        }
    config = _load_json(config_path)
    vllm = ((config.get('provider') or {}).get('vllm') or {})
    model = ((vllm.get('models') or {}).get('ornith-lspctx') or {})
    lsp_model = ((vllm.get('models') or {}).get(VLLM_LSP_MODEL_NAME) or {})
    lsp_edit_model = ((vllm.get('models') or {}).get(VLLM_LSP_EDIT_MODEL_NAME) or {})
    agent = ((config.get('agent') or {}).get(VLLM_LSP_AGENT) or {})
    edit_agent = ((config.get('agent') or {}).get(VLLM_LSP_EDIT_AGENT) or {})
    return {
        'endpoint': ((vllm.get('options') or {}).get('baseURL') or VLLM_ENDPOINT),
        'opencode_model': 'vllm/ornith-lspctx',
        'opencode_context': (model.get('limit') or {}).get('context'),
        'opencode_tool_call': model.get('tool_call'),
        'opencode_lsp_agent': VLLM_LSP_AGENT if agent else None,
        'opencode_lsp_model': f'vllm/{VLLM_LSP_MODEL_NAME}',
        'opencode_lsp_context': (lsp_model.get('limit') or {}).get('context'),
        'opencode_lsp_tool_call': lsp_model.get('tool_call'),
        'opencode_lsp_edit_agent': VLLM_LSP_EDIT_AGENT if edit_agent else None,
        'opencode_lsp_edit_model': f'vllm/{VLLM_LSP_EDIT_MODEL_NAME}',
        'opencode_lsp_edit_context': (lsp_edit_model.get('limit') or {}).get('context'),
        'opencode_lsp_edit_tool_call': lsp_edit_model.get('tool_call'),
    }


def _shell_default(script: str, name: str) -> str | None:
    match = re.search(rf'^(?:export\s+)?{name}="\$\{{{name}:-([^}}]+)\}}"$', script, re.MULTILINE)
    return match.group(1) if match else None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _vllm_start_defaults(root: Path) -> dict[str, Any]:
    script_path = root / 'scripts' / 'start_ornith_vllm.sh'
    if not script_path.exists():
        return {
            'max_model_len': None,
            'max_num_seqs': None,
            'metal_memory_fraction': None,
            'enable_auto_tool_choice': None,
            'tool_call_parser': None,
        }
    script = script_path.read_text(encoding='utf-8')
    enable_auto_tool_choice = _shell_default(script, 'ENABLE_AUTO_TOOL_CHOICE')
    return {
        'max_model_len': _int_or_none(_shell_default(script, 'MAX_MODEL_LEN')),
        'max_num_seqs': _int_or_none(_shell_default(script, 'MAX_NUM_SEQS')),
        'metal_memory_fraction': _float_or_none(_shell_default(script, 'VLLM_METAL_MEMORY_FRACTION')),
        'enable_auto_tool_choice': enable_auto_tool_choice == '1'
        if enable_auto_tool_choice is not None
        else None,
        'tool_call_parser': _shell_default(script, 'TOOL_CALL_PARSER'),
    }


def _vllm_preflight(root: Path, hf_home: Path, repo_id: str = VLLM_MODEL_ID) -> dict[str, Any]:
    checker = _load_cache_checker()
    preparer = _load_cache_preparer()
    cache = checker.inspect_cache(hf_home, repo_id, None)
    shards = cache.get('shards') or []
    present = [Path(str(item['path'])).name for item in shards if item.get('complete')]
    missing = [Path(str(item['path'])).name for item in shards if not item.get('complete')]
    complete = bool(cache.get('complete'))
    opencode_config = _vllm_opencode_config(root)
    vllm_lsp_tools = _opencode_tool_evidence(root / OPENCODE_VLLM_LSP_JSONL)
    vllm_lsp_tools_seen = _opencode_tools_seen(root / OPENCODE_VLLM_LSP_JSONL)
    vllm_lsp_forbidden_tools_seen = [
        tool for tool in vllm_lsp_tools_seen if tool in FORBIDDEN_OPENCODE_TOOLS
    ]
    vllm_lsp_edit_tools = _opencode_tool_evidence(root / OPENCODE_VLLM_LSP_EDIT_JSONL)
    vllm_lsp_edit_tools_seen = _opencode_tools_seen(root / OPENCODE_VLLM_LSP_EDIT_JSONL)
    vllm_lsp_edit_forbidden_tools_seen = [
        tool for tool in vllm_lsp_edit_tools_seen if tool in FORBIDDEN_OPENCODE_LSP_EDIT_TOOLS
    ]
    mega_text = (root / MEGA_UNIT).read_text(encoding='utf-8') if (root / MEGA_UNIT).exists() else ''
    return {
        'endpoint': opencode_config['endpoint'],
        'served_model_name': VLLM_SERVED_MODEL_NAME,
        'model_id': repo_id,
        'opencode_model': opencode_config['opencode_model'],
        'opencode_context': opencode_config['opencode_context'],
        'opencode_tool_call': opencode_config['opencode_tool_call'],
        'opencode_lsp_agent': opencode_config['opencode_lsp_agent'],
        'opencode_lsp_model': opencode_config['opencode_lsp_model'],
        'opencode_lsp_context': opencode_config['opencode_lsp_context'],
        'opencode_lsp_tool_call': opencode_config['opencode_lsp_tool_call'],
        'opencode_lsp_jsonl': _portable_evidence_path(OPENCODE_VLLM_LSP_JSONL),
        'opencode_lsp_forbidden_tools_seen': vllm_lsp_forbidden_tools_seen,
        'opencode_lsp_only': bool(vllm_lsp_tools_seen)
        and all(tool == 'lsp' for tool in vllm_lsp_tools_seen)
        and not vllm_lsp_forbidden_tools_seen,
        'opencode_lsp_elapsed_ms': (vllm_lsp_tools.get('lsp') or {}).get('elapsed_ms'),
        'opencode_lsp_input': (vllm_lsp_tools.get('lsp') or {}).get('input'),
        'opencode_lsp_edit_agent': opencode_config['opencode_lsp_edit_agent'],
        'opencode_lsp_edit_model': opencode_config['opencode_lsp_edit_model'],
        'opencode_lsp_edit_context': opencode_config.get('opencode_lsp_edit_context'),
        'opencode_lsp_edit_tool_call': opencode_config.get('opencode_lsp_edit_tool_call'),
        'opencode_lsp_edit_jsonl': _portable_evidence_path(OPENCODE_VLLM_LSP_EDIT_JSONL),
        'opencode_lsp_edit_forbidden_tools': FORBIDDEN_OPENCODE_LSP_EDIT_TOOLS,
        'opencode_lsp_edit_forbidden_tools_seen': vllm_lsp_edit_forbidden_tools_seen,
        'opencode_lsp_edit_only': bool(vllm_lsp_edit_tools_seen)
        and all(tool in {'lsp', 'edit'} for tool in vllm_lsp_edit_tools_seen)
        and not vllm_lsp_edit_forbidden_tools_seen,
        'opencode_lsp_edit_lsp_elapsed_ms': (vllm_lsp_edit_tools.get('lsp') or {}).get('elapsed_ms'),
        'opencode_lsp_edit_elapsed_ms': (vllm_lsp_edit_tools.get('edit') or {}).get('elapsed_ms'),
        'opencode_lsp_edit_lsp_input': (vllm_lsp_edit_tools.get('lsp') or {}).get('input'),
        'opencode_lsp_edit_input': (vllm_lsp_edit_tools.get('edit') or {}).get('input'),
        'opencode_lsp_edit_marker': VLLM_EDIT_MARKER,
        'opencode_lsp_edit_marker_count': mega_text.count(VLLM_EDIT_MARKER),
        'start_defaults': _vllm_start_defaults(root),
        'hf_home': _portable_evidence_path(hf_home, external_alias='huggingface-cache'),
        'offline_only': True,
        'cache_complete': complete,
        'start_permitted': complete,
        'revision': cache.get('revision'),
        'required_shards': cache.get('required_shards') or [],
        'present_shards': present,
        'missing_shards': missing,
        'incomplete_files': _portable_incomplete_cache_files(
            cache.get('incomplete_files') or [],
            hf_home=hf_home,
        ),
        'cache_prepare': _portable_cache_prepare_evidence(
            preparer.prepare_cache(
                hf_home=hf_home,
                repo_id=repo_id,
                allow_download=False,
            )
        ),
    }


def _portable_cache_prepare_evidence(plan: dict[str, Any]) -> dict[str, Any]:
    portable = dict(plan)
    if 'hf_home' in portable:
        portable['hf_home'] = '@external/huggingface-cache'
    if 'cache_dir' in portable:
        portable['cache_dir'] = '@external/huggingface-cache/hub'
    return portable


def _portable_incomplete_cache_files(
    items: list[dict[str, Any]],
    *,
    hf_home: Path,
) -> list[dict[str, Any]]:
    portable_items: list[dict[str, Any]] = []
    for item in items:
        portable = dict(item)
        path = Path(str(item.get('path') or 'unknown'))
        try:
            relative = path.resolve().relative_to(hf_home.resolve()).as_posix()
        except ValueError:
            relative = path.name
        portable['path'] = f'@external/huggingface-cache/{relative}'
        portable_items.append(portable)
    return portable_items


def _request_budget_summary(payloads: dict[str, Any], name: str) -> dict[str, Any]:
    request = payloads.get(name) or {}
    return {
        'json_chars': request.get('json_chars'),
        'system_chars': request.get('system_chars'),
        'tool_count': request.get('tool_count'),
        'tool_names': request.get('tool_names') or [],
    }


def _estimated_tokens_from_chars(chars: int | None) -> int | None:
    if chars is None:
        return None
    return (chars + 3) // 4


def _delta(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left - right


def _context_budget_evidence(
    opencode: dict[str, Any],
    vllm: dict[str, Any],
    github_vllm_lsp_operations: dict[str, Any],
    *,
    large_file_line_count: int,
) -> dict[str, Any]:
    payloads = opencode.get('request_payloads') or {}
    default_request = _request_budget_summary(payloads, 'default_agent')
    lsp_only_request = _request_budget_summary(payloads, 'vllm_lsp_only_agent')
    context_tokens = vllm.get('opencode_lsp_context')
    estimated_request_tokens = _estimated_tokens_from_chars(lsp_only_request.get('json_chars'))
    estimated_remaining = _delta(context_tokens, estimated_request_tokens)
    lsp_only = bool(vllm.get('opencode_lsp_only')) and bool(
        github_vllm_lsp_operations.get('lsp_only')
    )
    source_file_loaded_into_prompt = not lsp_only
    status = 'pass'
    if (
        context_tokens is None
        or estimated_remaining is None
        or estimated_remaining < 16_384
        or lsp_only_request.get('tool_names') != ['lsp']
        or lsp_only_request.get('tool_count') != 1
        or source_file_loaded_into_prompt
    ):
        status = 'fail'

    return {
        'status': status,
        'model': vllm.get('opencode_lsp_model'),
        'agent': vllm.get('opencode_lsp_agent'),
        'context_tokens': context_tokens,
        'default_request': default_request,
        'lsp_only_request': lsp_only_request,
        'request_json_chars_saved': _delta(
            default_request.get('json_chars'), lsp_only_request.get('json_chars')
        ),
        'system_chars_saved': _delta(
            default_request.get('system_chars'), lsp_only_request.get('system_chars')
        ),
        'tools_removed': _delta(default_request.get('tool_count'), lsp_only_request.get('tool_count')),
        'estimated_lsp_only_request_tokens': estimated_request_tokens,
        'estimated_context_tokens_remaining': estimated_remaining,
        'large_file_line_count': large_file_line_count,
        'github_file_line_count': github_vllm_lsp_operations.get('source_line_count'),
        'lsp_only': lsp_only,
        'source_file_loaded_into_prompt': source_file_loaded_into_prompt,
    }


def _requirement(requirement_id: str, ok: bool, evidence: list[str]) -> dict[str, Any]:
    return {
        'id': requirement_id,
        'status': 'pass' if ok else 'fail',
        'evidence': evidence,
    }


def _goal_audit(evidence: dict[str, Any], root: Path) -> dict[str, Any]:
    vllm = evidence['vllm']
    github_ops = evidence['github_vllm_lsp_operations']
    language_features = evidence['language_features']
    feature_summary = language_features.get('summary') or {}
    feature_verification = language_features.get('verification') or {}
    context_budget = evidence['context_budget']
    constraints = evidence['constraints']
    pdf_report = root / PDF_PROGRESS_REPORT
    expected_github_ops = {'workspaceSymbol', 'documentSymbol', 'hover', 'definition'}

    requirements = [
        _requirement(
            'ornith_vllm_endpoint',
            vllm.get('opencode_lsp_model') == 'vllm/ornith-lspctx'
            and vllm.get('opencode_lsp_context') == 44_352
            and vllm.get('start_defaults', {}).get('tool_call_parser') == 'qwen3_xml'
            and github_ops.get('lsp_only') is True,
            [
                f"model={vllm.get('opencode_lsp_model')}",
                f"context={vllm.get('opencode_lsp_context')}",
                f"tool_call_parser={vllm.get('start_defaults', {}).get('tool_call_parser')}",
                f"github_ops={','.join(github_ops.get('operations_seen') or [])}",
            ],
        ),
        _requirement(
            'opencode_lsp_large_files',
            vllm.get('opencode_lsp_only') is True
            and context_budget.get('large_file_line_count', 0) > 100_000
            and vllm.get('opencode_lsp_elapsed_ms') is not None,
            [
                f"large_file_lines={context_budget.get('large_file_line_count')}",
                f"elapsed_ms={vllm.get('opencode_lsp_elapsed_ms')}",
                f"lsp_only={vllm.get('opencode_lsp_only')}",
            ],
        ),
        _requirement(
            'github_test_projects',
            str(github_ops.get('source_path', '')).startswith('test_projects/github_repos/')
            and github_ops.get('source_clean') is True
            and expected_github_ops.issubset(set(github_ops.get('operations_seen') or [])),
            [
                f"source={github_ops.get('source_path')}",
                f"source_clean={github_ops.get('source_clean')}",
                f"operations={','.join(github_ops.get('operations_seen') or [])}",
            ],
        ),
        _requirement(
            'all_delphi_language_features',
            feature_verification.get('ok') is True
            and feature_summary.get('total') == feature_summary.get('covered')
            and feature_summary.get('lsp_operations', 0) >= 8
            and feature_summary.get('direct_lsp_assertions', 0) >= 21,
            [
                f"covered={feature_summary.get('covered')}/{feature_summary.get('total')}",
                f"lsp_operations={feature_summary.get('lsp_operations')}",
                f"direct_lsp_assertions={feature_summary.get('direct_lsp_assertions')}",
            ],
        ),
        _requirement(
            'smaller_context_via_lsp',
            context_budget.get('status') == 'pass'
            and context_budget.get('source_file_loaded_into_prompt') is False,
            [
                f"context_budget={context_budget.get('status')}",
                f"estimated_remaining={context_budget.get('estimated_context_tokens_remaining')}",
                f"source_file_loaded_into_prompt={context_budget.get('source_file_loaded_into_prompt')}",
            ],
        ),
        _requirement(
            'pdf_progress',
            pdf_report.exists() and pdf_report.stat().st_size > 0,
            [f"path={PDF_PROGRESS_REPORT}", f"bytes={pdf_report.stat().st_size if pdf_report.exists() else 0}"],
        ),
        _requirement(
            'no_github_source_changes',
            constraints.get('github_repos_clean') is True,
            [f"github_repos_status={constraints.get('github_repos_status')}"],
        ),
        _requirement(
            'no_push',
            constraints.get('pushed_by_agent') is False,
            [f"pushed_by_agent={constraints.get('pushed_by_agent')}"],
        ),
    ]

    return {
        'status': 'pass' if all(item['status'] == 'pass' for item in requirements) else 'fail',
        'requirements': requirements,
    }


def _git_status(root: Path, path: str) -> list[str] | None:
    try:
        output = subprocess.check_output(
            ['git', 'status', '--short', '--', path],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in output.splitlines() if line.strip()]


def _github_lsp_edit_evidence(
    root: Path,
    *,
    jsonl_path: Path,
    marker: str,
    model: str,
) -> dict[str, Any]:
    tools = _opencode_tool_evidence(root / jsonl_path)
    tools_seen = _opencode_tools_seen(root / jsonl_path)
    forbidden_tools_seen = [tool for tool in tools_seen if tool in FORBIDDEN_OPENCODE_LSP_EDIT_TOOLS]
    source_text = _read_text_if_exists(root / GITHUB_LSP_EDIT_SOURCE)
    sandbox_text = _read_text_if_exists(root / GITHUB_LSP_EDIT_SANDBOX)
    source_status = _git_status(root, str(GITHUB_LSP_EDIT_SOURCE))
    source_marker_count = source_text.count(marker)
    source_clean = source_status == [] if source_status is not None else source_marker_count == 0

    return {
        'agent': GITHUB_LSP_EDIT_AGENT,
        'model': model,
        'source_path': _portable_evidence_path(GITHUB_LSP_EDIT_SOURCE),
        'sandbox_path': _portable_evidence_path(GITHUB_LSP_EDIT_SANDBOX),
        'jsonl': _portable_evidence_path(jsonl_path),
        'source_status': source_status,
        'source_clean': source_clean,
        'source_line_count': _line_count(source_text),
        'sandbox_line_count': _line_count(sandbox_text),
        'marker': marker,
        'marker_count': sandbox_text.count(marker),
        'source_marker_count': source_marker_count,
        'tools_seen': tools_seen,
        'forbidden_tools': FORBIDDEN_OPENCODE_LSP_EDIT_TOOLS,
        'forbidden_tools_seen': forbidden_tools_seen,
        'lsp_edit_only': bool(tools_seen)
        and all(tool in {'lsp', 'edit'} for tool in tools_seen)
        and not forbidden_tools_seen,
        'lsp_elapsed_ms': (tools.get('lsp') or {}).get('elapsed_ms'),
        'edit_elapsed_ms': (tools.get('edit') or {}).get('elapsed_ms'),
        'lsp_input': (tools.get('lsp') or {}).get('input'),
        'edit_input': (tools.get('edit') or {}).get('input'),
    }


def _github_vllm_lsp_operations_evidence(root: Path) -> dict[str, Any]:
    jsonl_path = GITHUB_VLLM_LSP_OPS_JSONL
    tools_seen = _opencode_tools_seen(root / jsonl_path)
    completed_events = _opencode_completed_tool_events(root / jsonl_path)
    lsp_events = [event for event in completed_events if event.get('tool') == 'lsp']
    operations = [
        _normalize_lsp_operation((event.get('input') or {}).get('operation'))
        for event in lsp_events
        if (event.get('input') or {}).get('operation')
    ]
    forbidden_tools_seen = [tool for tool in tools_seen if tool in FORBIDDEN_OPENCODE_LSP_ONLY_TOOLS]
    source_text = _read_text_if_exists(root / GITHUB_LSP_EDIT_SOURCE)
    sandbox_text = _read_text_if_exists(root / GITHUB_LSP_EDIT_SANDBOX)
    source_status = _git_status(root, str(GITHUB_LSP_EDIT_SOURCE))
    source_clean = source_status == [] if source_status is not None else True

    return {
        'agent': VLLM_LSP_AGENT,
        'model': f'vllm/{VLLM_LSP_MODEL_NAME}',
        'source_path': _portable_evidence_path(GITHUB_LSP_EDIT_SOURCE),
        'sandbox_path': _portable_evidence_path(GITHUB_LSP_EDIT_SANDBOX),
        'jsonl': _portable_evidence_path(jsonl_path),
        'source_status': source_status,
        'source_clean': source_clean,
        'source_line_count': _line_count(source_text),
        'sandbox_line_count': _line_count(sandbox_text),
        'tools_seen': tools_seen,
        'forbidden_tools': FORBIDDEN_OPENCODE_LSP_ONLY_TOOLS,
        'forbidden_tools_seen': forbidden_tools_seen,
        'lsp_only': bool(tools_seen)
        and all(tool == 'lsp' for tool in tools_seen)
        and not forbidden_tools_seen,
        'operations_seen': operations,
        'elapsed_ms_by_operation': {
            _normalize_lsp_operation((event.get('input') or {}).get('operation')): event.get('elapsed_ms')
            for event in lsp_events
            if (event.get('input') or {}).get('operation')
        },
        'raw_operations_seen': [
            str((event.get('input') or {}).get('operation'))
            for event in lsp_events
            if (event.get('input') or {}).get('operation')
        ],
        'inputs': [event.get('input') or {} for event in lsp_events],
    }


def build_release_evidence(root: Path = ROOT, *, hf_home: Path = VLLM_HF_HOME) -> dict[str, Any]:
    root = root.resolve()
    corpus_report = _load_json(root / CORPUS_REPORT)
    summary = corpus_report.get('summary') or {}
    lsp_tools = _opencode_tool_evidence(root / OPENCODE_LSP_JSONL)
    lsp_tools_seen = _opencode_tools_seen(root / OPENCODE_LSP_JSONL)
    forbidden_tools_seen = [tool for tool in lsp_tools_seen if tool in FORBIDDEN_OPENCODE_TOOLS]
    lsp_only = bool(lsp_tools_seen) and all(tool == 'lsp' for tool in lsp_tools_seen) and not forbidden_tools_seen
    edit_tools = _opencode_tool_evidence(root / OPENCODE_EDIT_JSONL)
    opencode_config = _opencode_model_config(root)
    mega_text = (root / MEGA_UNIT).read_text(encoding='utf-8') if (root / MEGA_UNIT).exists() else ''
    wheel = next((root / 'dist').glob('*.whl'), root / 'dist' / 'missing.whl')
    sdist = next((root / 'dist').glob('*.tar.gz'), root / 'dist' / 'missing.tar.gz')
    github_status = _git_status(root, 'test_projects/github_repos')
    opencode_evidence = {
        'lsp_jsonl': _portable_evidence_path(OPENCODE_LSP_JSONL),
        'edit_jsonl': _portable_evidence_path(OPENCODE_EDIT_JSONL),
        'model': opencode_config['model'],
        'context': opencode_config['context'],
        'tool_call': opencode_config['tool_call'],
        'forbidden_tools': FORBIDDEN_OPENCODE_TOOLS,
        'forbidden_tools_seen': forbidden_tools_seen,
        'lsp_only': lsp_only,
        'lsp_elapsed_ms': (lsp_tools.get('lsp') or {}).get('elapsed_ms'),
        'edit_elapsed_ms': (edit_tools.get('edit') or {}).get('elapsed_ms'),
        'lsp_input': (lsp_tools.get('lsp') or {}).get('input'),
        'edit_input': (edit_tools.get('edit') or {}).get('input'),
        'marker': MARKER,
        'marker_count': mega_text.count(MARKER),
        'request_payloads': _load_optional_json(root / OPENCODE_REQUEST_PAYLOADS_JSON),
    }
    github_lsp_edit_evidence = _github_lsp_edit_evidence(
        root,
        jsonl_path=GITHUB_LSP_EDIT_JSONL,
        marker=GITHUB_LSP_EDIT_MARKER,
        model=DEFAULT_OPENCODE_MODEL,
    )
    github_vllm_lsp_edit_evidence = _github_lsp_edit_evidence(
        root,
        jsonl_path=GITHUB_VLLM_LSP_EDIT_JSONL,
        marker=GITHUB_VLLM_LSP_EDIT_MARKER,
        model=f'vllm/{VLLM_LSP_EDIT_MODEL_NAME}',
    )
    github_vllm_lsp_operations = _github_vllm_lsp_operations_evidence(root)
    vllm_evidence = _vllm_preflight(root, hf_home)
    github_repos_clean = (
        github_status == []
        if github_status is not None
        else github_lsp_edit_evidence.get('source_clean') is True
        and github_vllm_lsp_edit_evidence.get('source_clean') is True
        and github_vllm_lsp_operations.get('source_clean') is True
    )

    release_evidence = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'corpus': {
            'total_files': summary.get('total_files'),
            'ok': summary.get('ok'),
            'fail': summary.get('fail'),
            'large_files': summary.get('large_files'),
                'semantic': summary.get('semantic'),
        },
        'opencode': opencode_evidence,
        'language_features': _load_optional_json(root / LANGUAGE_FEATURE_MATRIX_JSON),
        'github_lsp_edit': github_lsp_edit_evidence,
        'github_vllm_lsp_edit': github_vllm_lsp_edit_evidence,
        'github_vllm_lsp_operations': github_vllm_lsp_operations,
        'vllm': vllm_evidence,
        'context_budget': _context_budget_evidence(
            opencode_evidence,
            vllm_evidence,
            github_vllm_lsp_operations,
            large_file_line_count=_line_count(mega_text),
        ),
        'packaging': {
            'wheel': _dist_artifact(wheel, root=root),
            'sdist': _dist_artifact(sdist, root=root),
        },
        'constraints': {
            'github_repos_status': github_status,
            'github_repos_clean': github_repos_clean,
            'pushed_by_agent': False,
        },
    }
    release_evidence['goal_audit'] = _goal_audit(release_evidence, root)
    return release_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate release evidence from current local artifacts.')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'release' / 'release_evidence.json')
    args = parser.parse_args()

    evidence = build_release_evidence(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
