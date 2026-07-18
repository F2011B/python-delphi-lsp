from __future__ import annotations

import importlib.util
import json
import os
import signal
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
    assert '--dangerously-skip-permissions' not in command


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


def test_build_opencode_command_accepts_explicit_binary() -> None:
    probe = _load_probe_module()

    command = probe.build_opencode_command(
        opencode='/opt/release/opencode',
        title='release-gate',
        model='openrouter/google/gemma-4-31b-it',
        agent='python-delphi-lsp',
        cwd=str(ROOT),
        prompt='use the semantic tools',
    )

    assert command[0] == '/opt/release/opencode'


def test_probe_uses_portable_threaded_stdout_and_process_shutdown() -> None:
    script = SCRIPT.read_text(encoding='utf-8')

    assert 'import selectors' not in script
    assert 'import signal' in script
    assert 'signal.SIGTERM' in script
    assert 'os.killpg' in script
    assert 'start_new_session=not inherit_process_group' in script
    assert 'threading.Thread' in script
    assert 'queue.Queue' in script


def test_stop_process_kills_group_even_when_leader_already_exited(monkeypatch) -> None:
    probe = _load_probe_module()
    group_alive = True
    signals: list[int] = []

    class StubbornProcess:
        pid = 4141

        def poll(self):
            return 0

        def wait(self, timeout=None):
            del timeout
            return 0

    def fake_killpg(pid: int, sig: int) -> None:
        nonlocal group_alive
        assert pid == 4141
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append(sig)
        if sig == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(probe.os, 'killpg', fake_killpg)

    process = StubbornProcess()

    probe._stop_process(process, timeout=0.01)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert group_alive is False


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


def test_evidence_from_jsonl_can_require_delphi_codebase_action() -> None:
    probe = _load_probe_module()
    lines = [
        json.dumps(
            {
                'type': 'tool_use',
                'part': {
                    'tool': 'delphi_codebase',
                    'state': {
                        'status': 'completed',
                        'input': {'action': 'focus', 'target_id': 'target_v2_123'},
                        'output': 'Value := Value + 40;',
                        'time': {'start': 2000, 'end': 2450},
                    },
                },
            }
        ),
    ]

    requirement = probe.parse_tool_requirement('delphi_codebase.focus:target_id')
    evidence = probe.evidence_from_jsonl(
        lines,
        tool=requirement.tool,
        operation=requirement.operation,
        expected=requirement.expected,
    )

    assert requirement.operation == 'focus'
    assert evidence is not None
    assert evidence.elapsed_ms == 450
    assert evidence.tool_input['action'] == 'focus'


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
            'print(lines[1], flush=True)'
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


def test_run_probe_waits_for_required_final_response_after_tools(tmp_path, capsys) -> None:
    probe = _load_probe_module()
    events = [
        {
            'type': 'tool_use',
            'part': {
                'tool': 'delphi_codebase',
                'state': {
                    'status': 'completed',
                    'input': {'action': 'inspect', 'detail': 'body'},
                    'output': 'Value := Value + 40',
                    'time': {'start': 1000, 'end': 1093},
                },
            },
        },
        {
            'type': 'text',
            'part': {'text': 'src/Mega100kUnit.pas:117464 contains Value := Value + 40.'},
        },
    ]
    event_lines = [json.dumps(event) for event in events]
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            f'lines = {event_lines!r}; '
            'print(lines[0], flush=True); '
            'print(lines[1], flush=True)'
        ),
    ]
    args = Namespace(
        title='final-response-test',
        model='vllm/ornith-lspctx',
        agent='python-delphi-lsp',
        prompt='inspect and explain',
        cwd=str(ROOT),
        output=str(tmp_path / 'probe.jsonl'),
        timeout=2.0,
        tool='delphi_codebase',
        expected='Value := Value + 40',
        require_tool=['delphi_codebase.inspect:Value := Value + 40'],
        require_final=['src/Mega100kUnit.pas', '117464', 'Value := Value + 40'],
        forbid_tool=['bash', 'read', 'grep'],
    )

    result = probe.run_probe(args)
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload['final_response'] == 'src/Mega100kUnit.pas:117464 contains Value := Value + 40.'
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
            'print(lines[1], flush=True)'
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
    assert len(lines) == 2
    assert json.loads(lines[0])['part']['tool'] == 'bash'


def test_run_probe_rejects_forbidden_tool_after_matching_final(tmp_path) -> None:
    probe = _load_probe_module()
    required = {
        'type': 'tool_use',
        'part': {
            'tool': 'lsp',
            'state': {
                'status': 'completed',
                'input': {'operation': 'workspaceSymbol'},
                'output': 'MegaProc02500',
            },
        },
    }
    final = {'type': 'text', 'part': {'text': 'MegaProc02500 found.'}}
    forbidden = {
        'type': 'tool_use',
        'part': {
            'tool': 'bash',
            'state': {
                'status': 'completed',
                'input': {'command': 'cat source'},
                'output': 'source',
            },
        },
    }
    lines = [json.dumps(event) for event in (required, final, forbidden)]
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        f'lines={lines!r}; [print(line, flush=True) for line in lines]',
    ]
    output = tmp_path / 'late-forbidden.jsonl'
    args = Namespace(
        title='late-forbidden',
        model='test/model',
        prompt='probe',
        cwd=str(tmp_path),
        output=str(output),
        timeout=2.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
        require_final=['MegaProc02500'],
        forbid_tool=['bash'],
    )

    result = probe.run_probe(args)

    assert result == 2
    assert len(output.read_text(encoding='utf-8').splitlines()) == 3


def test_run_probe_rejects_additional_tool_in_exact_mode(tmp_path) -> None:
    probe = _load_probe_module()
    events = [
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol'},
                    'output': 'MegaProc02500',
                },
            },
        },
        {'type': 'text', 'part': {'text': 'MegaProc02500 found.'}},
        {
            'type': 'tool_use',
            'part': {
                'tool': 'edit',
                'state': {
                    'status': 'completed',
                    'input': {'filePath': 'x'},
                    'output': 'changed',
                },
            },
        },
    ]
    lines = [json.dumps(event) for event in events]
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        f'lines={lines!r}; [print(line, flush=True) for line in lines]',
    ]
    args = Namespace(
        title='late-extra',
        model='test/model',
        prompt='probe',
        cwd=str(tmp_path),
        output=None,
        timeout=2.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=['lsp.workspaceSymbol:MegaProc02500'],
        require_final=['MegaProc02500'],
        forbid_tool=[],
        exact_tools=True,
    )

    assert probe.run_probe(args) == 2


def test_run_probe_waits_for_normal_completion_after_final(tmp_path) -> None:
    probe = _load_probe_module()
    marker = tmp_path / 'completed'
    event = json.dumps(
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol'},
                    'output': 'MegaProc02500',
                },
            },
        }
    )
    final = json.dumps({'type': 'text', 'part': {'text': 'MegaProc02500 found.'}})
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import pathlib,time; '
            f'print({event!r}, flush=True); print({final!r}, flush=True); '
            'time.sleep(0.15); '
            f'pathlib.Path({str(marker)!r}).write_text("done")'
        ),
    ]
    args = Namespace(
        title='normal-completion',
        model='test/model',
        prompt='probe',
        cwd=str(tmp_path),
        output=None,
        timeout=2.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
        require_final=['MegaProc02500'],
        forbid_tool=[],
    )

    assert probe.run_probe(args) == 0
    assert marker.read_text(encoding='utf-8') == 'done'


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


def test_run_probe_drains_verbose_stderr_before_required_stdout(tmp_path, capsys) -> None:
    probe = _load_probe_module()
    event = json.dumps(
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol'},
                    'output': 'MegaProc02500',
                },
            },
        }
    )
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import sys; '
            'sys.stderr.write("diagnostic-" * 200000); '
            'sys.stderr.flush(); '
            f'print({event!r}, flush=True)'
        ),
    ]
    args = Namespace(
        title='stderr-drain-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp',
        cwd=str(tmp_path),
        output=None,
        timeout=2.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
    )

    result = probe.run_probe(args)
    captured = capsys.readouterr()

    assert result == 0
    assert captured.err == ''


def test_run_probe_surfaces_bounded_stderr_tail_on_timeout(tmp_path, capsys) -> None:
    probe = _load_probe_module()
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import sys, time; '
            'sys.stderr.write("discard-me-" * 50000); '
            'sys.stderr.write("USEFUL-STDERR-TAIL\\n"); '
            'sys.stderr.flush(); '
            'time.sleep(5)'
        ),
    ]
    args = Namespace(
        title='stderr-tail-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp',
        cwd=str(tmp_path),
        output=None,
        timeout=0.4,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
    )

    result = probe.run_probe(args)
    stderr = capsys.readouterr().err

    assert result == 1
    assert 'USEFUL-STDERR-TAIL' in stderr
    assert len(stderr) <= probe._STDERR_TAIL_CHARS + 1


def test_run_probe_propagates_isolated_npm_cache(tmp_path, monkeypatch) -> None:
    probe = _load_probe_module()
    npm_cache = tmp_path / 'isolated-npm-cache'
    monkeypatch.delenv('NPM_CONFIG_CACHE', raising=False)
    event = json.dumps(
        {
            'type': 'tool_use',
            'part': {
                'tool': 'lsp',
                'state': {
                    'status': 'completed',
                    'input': {'operation': 'workspaceSymbol'},
                    'output': 'MegaProc02500',
                },
            },
        }
    )
    probe.build_opencode_command = lambda **_kwargs: [
        sys.executable,
        '-c',
        (
            'import os; '
            f'assert os.environ.get("NPM_CONFIG_CACHE") == {str(npm_cache)!r}; '
            f'print({event!r}, flush=True)'
        ),
    ]
    args = Namespace(
        title='npm-cache-test',
        model='ollama/ornith-lspctx',
        prompt='use lsp',
        cwd=str(tmp_path),
        output=None,
        timeout=2.0,
        tool='lsp',
        expected='MegaProc02500',
        require_tool=None,
        npm_cache=str(npm_cache),
    )

    result = probe.run_probe(args)

    assert result == 0
    assert npm_cache.is_dir()
    assert 'NPM_CONFIG_CACHE' not in os.environ
