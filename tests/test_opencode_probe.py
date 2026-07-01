from __future__ import annotations

import importlib.util
import json
import sys
import time
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'run_opencode_lsp_probe.py'


def _load_probe_module():
    spec = importlib.util.spec_from_file_location('run_opencode_lsp_probe', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_opencode_command_uses_json_and_explicit_title() -> None:
    probe = _load_probe_module()

    command = probe.build_opencode_command(
        title='lsp-fast',
        model='ollama/ornith-lspctx',
        cwd=str(ROOT),
        prompt='use lsp',
    )

    assert command[:2] == ['opencode', 'run']
    assert '--format' in command
    assert command[command.index('--format') + 1] == 'json'
    assert '--title' in command
    assert command[command.index('--title') + 1] == 'lsp-fast'
    assert command[command.index('--model') + 1] == 'ollama/ornith-lspctx'
    assert command[command.index('--dir') + 1] == str(ROOT)


def test_build_opencode_command_accepts_explicit_agent() -> None:
    probe = _load_probe_module()

    command = probe.build_opencode_command(
        title='vllm-lsp-fast',
        model='vllm/ornith-lspctx',
        agent='vllm-lsp',
        cwd=str(ROOT),
        prompt='use lsp',
    )

    assert '--agent' in command
    assert command[command.index('--agent') + 1] == 'vllm-lsp'
    assert command[command.index('--model') + 1] == 'vllm/ornith-lspctx'


def test_evidence_from_jsonl_extracts_completed_lsp_tool_timing() -> None:
    probe = _load_probe_module()
    line = json.dumps(
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                    'output': '[{"name":"MegaProc02500"}]',
                    'time': {'start': 1000, 'end': 1432},
                },
            },
        }
    )

    evidence = probe.evidence_from_jsonl([line], tool='lsp', expected='MegaProc02500')

    assert evidence is not None
    assert evidence.status == 'completed'
    assert evidence.elapsed_ms == 432
    assert evidence.contains_expected is True
    assert evidence.tool_input['operation'] == 'workspaceSymbol'


def test_parse_tool_requirement_accepts_lsp_operation_qualifier() -> None:
    probe = _load_probe_module()

    requirement = probe.parse_tool_requirement('lsp.workspaceSymbol:TSynPersistent')

    assert requirement.tool == 'lsp'
    assert requirement.operation == 'workspaceSymbol'
    assert requirement.expected == 'TSynPersistent'


def test_evidence_from_jsonl_can_require_lsp_operation() -> None:
    probe = _load_probe_module()
    lines = [
        json.dumps(
            {
                'type': 'tool_use',
                'part': {
                    'tool': 'lsp',
                    'state': {
                        'status': 'completed',
                        'input': {'operation': 'hover', 'filePath': 'mormot.core.base.pas'},
                        'output': 'TSynPersistent hover',
                        'time': {'start': 1000, 'end': 1040},
                    },
                },
            }
        ),
        json.dumps(
            {
                'type': 'tool_use',
                'part': {
                    'tool': 'lsp',
                    'state': {
                        'status': 'completed',
                        'input': {'operation': 'workspaceSymbol', 'query': 'TSynPersistent'},
                        'output': '[{"name":"TSynPersistent"}]',
                        'time': {'start': 2000, 'end': 2450},
                    },
                },
            }
        ),
    ]

    evidence = probe.evidence_from_jsonl(
        lines,
        tool='lsp',
        operation='workspaceSymbol',
        expected='TSynPersistent',
    )

    assert evidence is not None
    assert evidence.elapsed_ms == 450
    assert evidence.tool_input['operation'] == 'workspaceSymbol'


def test_evidences_from_jsonl_extracts_multiple_required_tools() -> None:
    probe = _load_probe_module()
    lines = [
        json.dumps(
            {
                'type': 'tool_use',
                'part': {
                    'tool': 'lsp',
                    'state': {
                        'status': 'completed',
                        'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                        'output': '[{"name":"MegaProc02500"}]',
                        'time': {'start': 1000, 'end': 1432},
                    },
                },
            }
        ),
        json.dumps(
            {
                'type': 'tool_use',
                'part': {
                    'tool': 'edit',
                    'state': {
                        'status': 'completed',
                        'input': {'filePath': 'Mega100kUnit.pas'},
                        'output': 'Edit applied successfully',
                        'time': {'start': 2000, 'end': 2400},
                    },
                },
            }
        ),
    ]

    evidences = probe.evidences_from_jsonl(
        lines,
        requirements=[
            probe.ToolRequirement(tool='lsp', expected='MegaProc02500'),
            probe.ToolRequirement(tool='edit', expected='Edit applied successfully'),
        ],
    )

    assert [evidence.tool for evidence in evidences] == ['lsp', 'edit']
    assert [evidence.elapsed_ms for evidence in evidences] == [432, 400]
    assert all(evidence.contains_expected for evidence in evidences)


def test_run_probe_waits_until_all_required_tools_are_seen(tmp_path) -> None:
    probe = _load_probe_module()
    events = [
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                    'output': '[{"name":"MegaProc02500"}]',
                    'time': {'start': 1000, 'end': 1432},
                },
            },
        },
        {
            'type': 'tool_use',
            'part': {
                'tool': 'edit',
                'state': {
                    'status': 'completed',
                    'input': {'filePath': 'Mega100kUnit.pas'},
                    'output': 'Edit applied successfully',
                    'time': {'start': 2000, 'end': 2400},
                },
            },
        },
    ]
    event_lines = [json.dumps(event) for event in events]
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import sys, time; '
            f'lines = {event_lines!r}; '
            'print(lines[0], flush=True); '
            'time.sleep(0.1); '
            'print(lines[1], flush=True); '
            'time.sleep(5)'
        ),
    ]
    args = Namespace(
        title='multi-tool-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp and edit',
        cwd=str(ROOT),
        output=str(tmp_path / 'probe.jsonl'),
        timeout=5.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=['lsp:MegaProc02500', 'edit:Edit applied successfully'],
    )

    started_at = time.perf_counter()
    result = probe.run_probe(args)
    elapsed = time.perf_counter() - started_at

    assert result == 0
    assert elapsed < 2.0
    assert len((tmp_path / 'probe.jsonl').read_text(encoding='utf-8').splitlines()) == 2


def test_run_probe_rejects_forbidden_tools_before_required_evidence(tmp_path) -> None:
    probe = _load_probe_module()
    events = [
        {
            'type': 'tool_use',
            'part': {
                'tool': 'bash',
                'state': {
                    'status': 'completed',
                    'input': {'command': 'grep MegaProc02500 Mega100kUnit.pas'},
                    'output': '117463:procedure MegaProc02500;',
                    'time': {'start': 1000, 'end': 1005},
                },
            },
        },
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                    'output': '[{"name":"MegaProc02500"}]',
                    'time': {'start': 2000, 'end': 2309},
                },
            },
        },
    ]
    event_lines = [json.dumps(event) for event in events]
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import sys, time; '
            f'lines = {event_lines!r}; '
            'print(lines[0], flush=True); '
            'time.sleep(0.1); '
            'print(lines[1], flush=True); '
            'time.sleep(5)'
        ),
    ]
    args = Namespace(
        title='forbidden-tool-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp only',
        cwd=str(ROOT),
        output=str(tmp_path / 'probe.jsonl'),
        timeout=5.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
        forbid_tool=['bash', 'read', 'glob'],
    )

    result = probe.run_probe(args)

    assert result == 2
    lines = (tmp_path / 'probe.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])['part']['tool'] == 'bash'


def test_run_probe_timeout_is_not_blocked_waiting_for_first_output(tmp_path) -> None:
    probe = _load_probe_module()
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        'import time; time.sleep(5)',
    ]
    args = Namespace(
        title='timeout-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp',
        cwd=str(ROOT),
        output=str(tmp_path / 'probe.jsonl'),
        timeout=0.2,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
    )

    started_at = time.perf_counter()
    result = probe.run_probe(args)
    elapsed = time.perf_counter() - started_at

    assert result == 1
    assert elapsed < 2.0
