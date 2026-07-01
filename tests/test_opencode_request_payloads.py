from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'summarize_opencode_request_payloads.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('summarize_opencode_request_payloads', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_summarize_request_payload_counts_prompt_and_tools(tmp_path) -> None:
    module = _load_module()
    request = tmp_path / 'request.json'
    user_content = [{'type': 'text', 'text': 'Find Symbol'}]
    body = {
        'messages': [
            {'role': 'system', 'content': 'system prompt'},
            {'role': 'user', 'content': user_content},
            {'role': 'assistant', 'content': 'previous answer'},
        ],
        'tools': [
            {'function': {'name': 'lsp'}},
            {'name': 'edit'},
        ],
    }
    request.write_text(json.dumps({'body': body}), encoding='utf-8')

    summary = module.summarize_request(request)

    assert summary['exists'] is True
    assert summary['file_bytes'] == request.stat().st_size
    assert summary['json_chars'] == len(json.dumps(body, ensure_ascii=False))
    assert summary['system_chars'] == len('system prompt')
    assert summary['user_chars'] == len(json.dumps(user_content, ensure_ascii=False))
    assert summary['other_message_chars'] == len('previous answer')
    assert summary['tool_count'] == 2
    assert summary['tool_names'] == ['lsp', 'edit']


def test_summarize_request_payload_accepts_string_body(tmp_path) -> None:
    module = _load_module()
    request = tmp_path / 'request.json'
    body = {
        'messages': [{'role': 'system', 'content': 's'}],
        'tools': [],
    }
    request.write_text(json.dumps({'body': json.dumps(body)}), encoding='utf-8')

    summary = module.summarize_request(request)

    assert summary['system_chars'] == 1
    assert summary['tool_count'] == 0


def test_default_capture_paths_include_github_vllm_lsp_edit_requests() -> None:
    module = _load_module()

    assert 'vllm_github_lsp_edit_first_request' in module.DEFAULT_CAPTURE_PATHS
    assert 'vllm_github_lsp_edit_second_request' in module.DEFAULT_CAPTURE_PATHS
    assert 'vllm_github_lsp_44k_request' in module.DEFAULT_CAPTURE_PATHS
    assert 'vllm_mega_lsp_44k_request' in module.DEFAULT_CAPTURE_PATHS
    assert 'vllm_mega_lsp_edit_44k_request' in module.DEFAULT_CAPTURE_PATHS
    assert 'vllm_github_lsp_edit_44k_request' in module.DEFAULT_CAPTURE_PATHS
    assert module.DEFAULT_CAPTURE_PATHS['vllm_github_lsp_edit_first_request'].name.endswith(
        '-0001-request.json'
    )
    assert module.DEFAULT_CAPTURE_PATHS['vllm_github_lsp_edit_second_request'].name.endswith(
        '-0002-request.json'
    )
    assert module.DEFAULT_CAPTURE_PATHS['vllm_github_lsp_44k_request'].name.endswith(
        '-0001-request.json'
    )
    assert module.DEFAULT_CAPTURE_PATHS['vllm_mega_lsp_44k_request'].name.endswith(
        '-0001-request.json'
    )
    assert module.DEFAULT_CAPTURE_PATHS['vllm_mega_lsp_edit_44k_request'].name.endswith(
        '-0001-request.json'
    )
    assert module.DEFAULT_CAPTURE_PATHS['vllm_github_lsp_edit_44k_request'].name.endswith(
        '-0001-request.json'
    )
