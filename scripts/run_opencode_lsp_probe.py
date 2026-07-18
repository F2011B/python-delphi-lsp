#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Iterable, TextIO


@dataclass
class ToolEvidence:
    tool: str
    status: str
    elapsed_ms: int | None
    contains_expected: bool
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolRequirement:
    tool: str
    expected: str
    operation: str | None = None


@dataclass(frozen=True)
class _ReaderFailure:
    error: BaseException


_STDOUT_EOF = object()
_STDERR_TAIL_CHARS = 16 * 1024


class _BoundedTextTail:
    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._text = ''
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        with self._lock:
            self._text = (self._text + text)[-self._max_chars :]

    def get(self) -> str:
        with self._lock:
            return self._text


def _read_stdout(stream: TextIO, output: queue.Queue[object]) -> None:
    try:
        for line in stream:
            output.put(line)
    except BaseException as error:
        output.put(_ReaderFailure(error))
    finally:
        output.put(_STDOUT_EOF)


def _read_stderr(stream: TextIO, tail: _BoundedTextTail) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            tail.append(chunk)
    except BaseException:
        return


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(
    proc: subprocess.Popen[Any],
    process_group_id: int,
    timeout: float,
) -> bool:
    deadline = monotonic() + timeout
    while True:
        proc.poll()
        if not _group_exists(process_group_id):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.01, remaining))


def _stop_process(proc: subprocess.Popen[Any], *, timeout: float = 5.0) -> None:
    process_group_id = proc.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait(timeout=timeout)
        return
    if not _wait_for_group_exit(proc, process_group_id, timeout):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_for_group_exit(proc, process_group_id, timeout):
            raise RuntimeError(f'OpenCode process group {process_group_id} survived SIGKILL')
    proc.wait(timeout=timeout)


def build_opencode_command(
    *,
    title: str,
    model: str,
    cwd: str,
    prompt: str,
    agent: str | None = None,
    opencode: str = 'opencode',
) -> list[str]:
    command = [
        opencode,
        'run',
        '--no-replay',
        '--title',
        title,
        '--dir',
        str(Path(cwd).resolve()),
        '--format',
        'json',
        '--model',
        model,
    ]
    if agent:
        command.extend(['--agent', agent])
    command.append(prompt)
    return command


def parse_tool_requirement(raw: str) -> ToolRequirement:
    tool_spec, separator, expected = raw.partition(':')
    if not separator or not tool_spec or not expected:
        raise argparse.ArgumentTypeError('tool requirements must use TOOL[:OPERATION]:EXPECTED or TOOL.OPERATION:EXPECTED')
    tool, dot, operation = tool_spec.partition('.')
    if dot and (not tool or not operation):
        raise argparse.ArgumentTypeError('operation-qualified requirements must use TOOL.OPERATION:EXPECTED')
    return ToolRequirement(tool=tool_spec if not dot else tool, expected=expected, operation=operation or None)


def requirements_from_args(args: argparse.Namespace) -> list[ToolRequirement]:
    raw_requirements = getattr(args, 'require_tool', None)
    if raw_requirements:
        return [parse_tool_requirement(raw) for raw in raw_requirements]
    return [ToolRequirement(tool=args.tool, expected=args.expected)]


def evidence_from_event(
    event: dict[str, Any],
    *,
    tool: str,
    expected: str,
    operation: str | None = None,
) -> ToolEvidence | None:
    if event.get('type') != 'tool_use':
        return None
    part = event.get('part') or {}
    if part.get('tool') != tool:
        return None
    state = part.get('state') or {}
    tool_input = state.get('input') or {}
    if operation is not None:
        input_operation = tool_input.get('operation')
        input_action = tool_input.get('action')
        if input_operation != operation and input_action != operation:
            return None
    timing = state.get('time') or {}
    start = timing.get('start')
    end = timing.get('end')
    elapsed_ms = end - start if isinstance(start, int) and isinstance(end, int) else None
    output = state.get('output')
    return ToolEvidence(
        tool=part.get('tool') or '',
        status=state.get('status') or '',
        elapsed_ms=elapsed_ms,
        contains_expected=expected in str(output),
        tool_input=tool_input,
    )


def evidence_from_jsonl(
    lines: Iterable[str],
    *,
    tool: str,
    expected: str,
    operation: str | None = None,
) -> ToolEvidence | None:
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        evidence = evidence_from_event(event, tool=tool, expected=expected, operation=operation)
        if evidence is not None:
            return evidence
    return None


def evidences_from_jsonl(
    lines: Iterable[str],
    *,
    requirements: list[ToolRequirement],
) -> list[ToolEvidence]:
    found: list[ToolEvidence] = []
    remaining = list(requirements)
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        for requirement in list(remaining):
            evidence = evidence_from_event(
                event,
                tool=requirement.tool,
                expected=requirement.expected,
                operation=requirement.operation,
            )
            if evidence is None:
                continue
            if evidence.status == 'completed' and evidence.contains_expected:
                found.append(evidence)
                remaining.remove(requirement)
                break
        if not remaining:
            break
    return found


def final_response_from_event(event: dict[str, Any], *, required: list[str]) -> str | None:
    if event.get('type') != 'text':
        return None
    text = str((event.get('part') or {}).get('text') or '').strip()
    if not text or not all(expected in text for expected in required):
        return None
    return text


def run_probe(args: argparse.Namespace) -> int:
    inherit_process_group = bool(getattr(args, 'inherit_process_group', False))
    command = build_opencode_command(
        title=args.title,
        model=args.model,
        cwd=args.cwd,
        prompt=args.prompt,
        agent=getattr(args, 'agent', None),
        opencode=getattr(args, 'opencode', 'opencode'),
    )
    env = os.environ.copy()
    env['OPENCODE_EXPERIMENTAL_LSP_TOOL'] = 'true'
    configured_npm_cache = getattr(args, 'npm_cache', None) or env.get('NPM_CONFIG_CACHE')
    npm_cache = Path(configured_npm_cache) if configured_npm_cache else Path(args.cwd) / '.opencode' / '.npm-cache'
    if not npm_cache.is_absolute():
        npm_cache = Path(args.cwd) / npm_cache
    npm_cache = npm_cache.expanduser().resolve()
    npm_cache.mkdir(parents=True, exist_ok=True)
    env['NPM_CONFIG_CACHE'] = str(npm_cache)
    output_path = Path(args.output) if args.output else None
    output_file = output_path.open('w', encoding='utf-8') if output_path is not None else None
    started = monotonic()
    requirements = requirements_from_args(args)
    remaining_requirements = list(requirements)
    forbidden_tools = set(getattr(args, 'forbid_tool', None) or [])
    forbidden_event: dict[str, Any] | None = None
    evidences: list[ToolEvidence] = []
    required_final = list(getattr(args, 'require_final', None) or [])
    final_response: str | None = None
    text_responses: list[str] = []
    tool_events: list[dict[str, Any]] = []
    timed_out = False
    reader_failed = False
    stdout_eof = False
    proc = subprocess.Popen(
        command,
        cwd=args.cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=not inherit_process_group,
    )
    stdout_events: queue.Queue[object] = queue.Queue()
    assert proc.stdout is not None
    assert proc.stderr is not None
    stderr_tail = _BoundedTextTail(_STDERR_TAIL_CHARS)
    stdout_reader = threading.Thread(
        target=_read_stdout,
        args=(proc.stdout, stdout_events),
        daemon=True,
        name='opencode-probe-stdout',
    )
    stderr_reader = threading.Thread(
        target=_read_stderr,
        args=(proc.stderr, stderr_tail),
        daemon=True,
        name='opencode-probe-stderr',
    )
    stdout_reader.start()
    stderr_reader.start()
    try:
        while True:
            remaining_time = args.timeout - (monotonic() - started)
            if remaining_time <= 0:
                timed_out = True
                break
            try:
                item = stdout_events.get(timeout=min(0.2, remaining_time))
            except queue.Empty:
                if proc.poll() is not None and (stdout_eof or not stdout_reader.is_alive()):
                    break
                continue
            if item is _STDOUT_EOF:
                stdout_eof = True
                if proc.poll() is not None:
                    break
                continue
            if isinstance(item, _ReaderFailure):
                reader_failed = True
                stdout_eof = True
                if proc.poll() is not None:
                    break
                continue
            assert isinstance(item, str)
            line = item
            if output_file is not None:
                output_file.write(line)
                output_file.flush()
            event = json.loads(line)
            if event.get('type') == 'tool_use':
                tool_events.append(event)
                part = event.get('part') or {}
                if part.get('tool') in forbidden_tools and forbidden_event is None:
                    forbidden_event = event
            for requirement in list(remaining_requirements):
                evidence = evidence_from_event(
                    event,
                    tool=requirement.tool,
                    expected=requirement.expected,
                    operation=requirement.operation,
                )
                if evidence is None:
                    continue
                if evidence.status == 'completed' and evidence.contains_expected:
                    evidences.append(evidence)
                    remaining_requirements.remove(requirement)
                    break
            if event.get('type') == 'text':
                text = str((event.get('part') or {}).get('text') or '').strip()
                if text:
                    text_responses.append(text)
    finally:
        if inherit_process_group:
            if proc.poll() is not None:
                proc.wait()
        else:
            _stop_process(proc)
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        if output_file is not None:
            output_file.close()

    if required_final and text_responses:
        terminal_text = text_responses[-1]
        if all(expected in terminal_text for expected in required_final):
            final_response = terminal_text

    exact_tool_error = False
    if getattr(args, 'exact_tools', False):
        if len(tool_events) != len(requirements):
            exact_tool_error = True
        else:
            for event, requirement in zip(tool_events, requirements, strict=True):
                evidence = evidence_from_event(
                    event,
                    tool=requirement.tool,
                    expected=requirement.expected,
                    operation=requirement.operation,
                )
                if evidence is None or evidence.status != 'completed' or not evidence.contains_expected:
                    exact_tool_error = True
                    break

    if forbidden_event is not None:
        part = forbidden_event.get('part') or {}
        state = part.get('state') or {}
        payload = {
            'forbidden_tool': part.get('tool'),
            'status': state.get('status'),
            'tool_input': state.get('input') or {},
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        stderr = stderr_tail.get().strip()
        if stderr:
            print(stderr, file=sys.stderr)
        return 2

    if exact_tool_error:
        print(
            json.dumps(
                {
                    'error': 'unexpected tool sequence',
                    'expected_count': len(requirements),
                    'observed_count': len(tool_events),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    if (
        timed_out
        or reader_failed
        or proc.returncode != 0
        or remaining_requirements
        or (required_final and final_response is None)
    ):
        stderr = stderr_tail.get().strip()
        if stderr:
            print(stderr, file=sys.stderr)
        return 1
    payload = {
        'evidences': [
            {
                'tool': evidence.tool,
                'status': evidence.status,
                'elapsed_ms': evidence.elapsed_ms,
                'contains_expected': evidence.contains_expected,
                'tool_input': evidence.tool_input,
            }
            for evidence in evidences
        ],
    }
    if len(evidences) == 1:
        payload.update(payload['evidences'][0])
    if final_response is not None:
        payload['final_response'] = final_response
    print(json.dumps(payload, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Run an opencode LSP probe and verify tool and final-answer evidence.')
    parser.add_argument('prompt')
    parser.add_argument('--cwd', default='.')
    parser.add_argument('--model', default='ollama/ornith-lspctx')
    parser.add_argument('--agent', help='Optional opencode agent, for example vllm-lsp.')
    parser.add_argument('--opencode', default='opencode', help='OpenCode executable to run.')
    parser.add_argument(
        '--inherit-process-group',
        action='store_true',
        help='Let an outer release wrapper own cleanup of this probe and its descendants.',
    )
    parser.add_argument('--title', default='delphi-lsp-probe')
    parser.add_argument('--tool', default='lsp')
    parser.add_argument('--expected', default='MegaProc02500')
    parser.add_argument(
        '--require-tool',
        action='append',
        help='Required evidence as TOOL:EXPECTED or TOOL.OPERATION:EXPECTED. Repeat to require multiple completed tool calls.',
    )
    parser.add_argument(
        '--forbid-tool',
        action='append',
        help='Fail immediately if this tool is used before the complete probe evidence is available. Repeatable.',
    )
    parser.add_argument(
        '--require-final',
        action='append',
        help='Text that must occur in one final response after all required tools complete. Repeatable.',
    )
    parser.add_argument('--timeout', type=float, default=45.0)
    parser.add_argument('--npm-cache', help='Writable npm cache for the isolated opencode probe process.')
    parser.add_argument(
        '--exact-tools',
        action='store_true',
        help='Reject tool calls beyond the exact ordered --require-tool sequence.',
    )
    parser.add_argument('--output', help='Optional JSONL copy of opencode stdout.')
    return run_probe(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
