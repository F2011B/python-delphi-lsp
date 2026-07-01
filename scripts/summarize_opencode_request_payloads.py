#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_PATHS = {
    'default_agent': Path('/tmp/opencode-vllm-capture/logs/1782869306208-0001-request.json'),
    'vllm_lsp_only_agent': Path('/tmp/opencode-vllm-capture/logs_vllm_lsp/1782871097266-0004-request.json'),
    'vllm_lsp_edit_agent_first_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_lsp_edit_retry/1782872672788-0001-request.json'
    ),
    'vllm_lsp_edit_agent_second_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_lsp_edit_retry/1782872719336-0002-request.json'
    ),
    'vllm_github_lsp_edit_first_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_github_edit/1782875283625-0001-request.json'
    ),
    'vllm_github_lsp_edit_second_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_github_edit/1782875327008-0002-request.json'
    ),
    'vllm_github_lsp_44k_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_github_lsp_44k/1782877030115-0001-request.json'
    ),
    'vllm_mega_lsp_44k_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_mega_lsp_44k/1782877097473-0001-request.json'
    ),
    'vllm_mega_lsp_edit_44k_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_mega_edit_44k_exact_edit/1782878313492-0001-request.json'
    ),
    'vllm_github_lsp_edit_44k_request': Path(
        '/tmp/opencode-vllm-capture/logs_vllm_github_edit_44k_neutral2/1782878732102-0001-request.json'
    ),
}


def _load_body(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    body = payload.get('body', payload) if isinstance(payload, dict) else payload
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError(f'{path} does not contain a JSON object request body')
    return body


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))


def _tool_name(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    function = tool.get('function')
    if isinstance(function, dict) and function.get('name'):
        return str(function['name'])
    if tool.get('name'):
        return str(tool['name'])
    return None


def summarize_request(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            'path': str(path),
            'exists': False,
            'file_bytes': 0,
            'json_chars': 0,
            'system_chars': 0,
            'user_chars': 0,
            'other_message_chars': 0,
            'tool_count': 0,
            'tool_names': [],
        }

    body = _load_body(path)
    system_chars = 0
    user_chars = 0
    other_message_chars = 0
    for message in body.get('messages') or []:
        if not isinstance(message, dict):
            continue
        chars = _content_chars(message.get('content', ''))
        role = message.get('role')
        if role == 'system':
            system_chars += chars
        elif role == 'user':
            user_chars += chars
        else:
            other_message_chars += chars

    tool_names = [
        name
        for name in (_tool_name(tool) for tool in (body.get('tools') or []))
        if name is not None
    ]
    return {
        'path': str(path),
        'exists': True,
        'file_bytes': path.stat().st_size,
        'json_chars': len(json.dumps(body, ensure_ascii=False)),
        'system_chars': system_chars,
        'user_chars': user_chars,
        'other_message_chars': other_message_chars,
        'tool_count': len(body.get('tools') or []),
        'tool_names': tool_names,
    }


def _parse_capture(raw: str) -> tuple[str, Path]:
    name, separator, value = raw.partition('=')
    if not separator or not name or not value:
        raise argparse.ArgumentTypeError('captures must use NAME=PATH')
    return name, Path(value)


def main() -> int:
    parser = argparse.ArgumentParser(description='Summarize opencode request payload capture files.')
    parser.add_argument(
        '--capture',
        action='append',
        type=_parse_capture,
        help='Named capture as NAME=PATH. Repeatable. Defaults to current local vLLM captures.',
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=ROOT / 'output' / 'release' / 'opencode_request_payloads.json',
    )
    args = parser.parse_args()

    captures = dict(args.capture) if args.capture else DEFAULT_CAPTURE_PATHS
    summary = {name: summarize_request(path) for name, path in captures.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
