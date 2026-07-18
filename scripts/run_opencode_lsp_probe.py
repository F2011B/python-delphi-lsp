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
from typing import Any, Iterable, Sequence, TextIO


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
_IS_WINDOWS = sys.platform == 'win32'
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
_SIGKILL = getattr(signal, 'SIGKILL', 9)
_WINDOWS_JOB_EXIT_CODE = 1
_WINDOWS_JOB_WAIT_TIMEOUT = 5.0
_WINDOWS_BOOTSTRAP_RELEASE = b'\x00'
_WINDOWS_BOOTSTRAP = (
    'import subprocess, sys\n'
    "if sys.stdin.buffer.read() != b'\\x00': raise SystemExit(125)\n"
    'try:\n'
    '    result = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL)\n'
    'except BaseException:\n'
    "    print('Windows process bootstrap could not start target', file=sys.stderr)\n"
    '    raise SystemExit(126)\n'
    'raise SystemExit(result.returncode)\n'
)


class _WindowsJobApi:
    """Small, lazy Win32 binding for kill-on-close Job Objects."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ('ReadOperationCount', ctypes.c_ulonglong),
                ('WriteOperationCount', ctypes.c_ulonglong),
                ('OtherOperationCount', ctypes.c_ulonglong),
                ('ReadTransferCount', ctypes.c_ulonglong),
                ('WriteTransferCount', ctypes.c_ulonglong),
                ('OtherTransferCount', ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', _BasicLimitInformation),
                ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ('TotalUserTime', ctypes.c_longlong),
                ('TotalKernelTime', ctypes.c_longlong),
                ('ThisPeriodTotalUserTime', ctypes.c_longlong),
                ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
                ('TotalPageFaultCount', wintypes.DWORD),
                ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD),
                ('TotalTerminatedProcesses', wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._ctypes = ctypes
        self._ExtendedLimitInformation = _ExtendedLimitInformation
        self._BasicAccountingInformation = _BasicAccountingInformation

        self._create_job_object = kernel32.CreateJobObjectW
        self._create_job_object.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._create_job_object.restype = wintypes.HANDLE
        self._set_information = kernel32.SetInformationJobObject
        self._set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._set_information.restype = wintypes.BOOL
        self._assign_process = kernel32.AssignProcessToJobObject
        self._assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._assign_process.restype = wintypes.BOOL
        self._terminate_job = kernel32.TerminateJobObject
        self._terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._terminate_job.restype = wintypes.BOOL
        self._query_information = kernel32.QueryInformationJobObject
        self._query_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._query_information.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    def _error(self, action: str) -> OSError:
        error_code = self._ctypes.get_last_error()
        detail = self._ctypes.FormatError(error_code).strip()
        return OSError(error_code, f'{action} failed: {detail}')

    def create_kill_on_close(self) -> int:
        handle = self._create_job_object(None, None)
        if not handle:
            raise self._error('CreateJobObjectW')
        handle_value = int(handle)
        information = self._ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not self._set_information(
            handle_value,
            9,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
        ):
            error = self._error('SetInformationJobObject')
            self._close_handle(handle_value)
            raise error
        return handle_value

    def assign(self, handle: int, process_handle: int) -> None:
        if not self._assign_process(handle, process_handle):
            raise self._error('AssignProcessToJobObject')

    def terminate(self, handle: int, exit_code: int) -> None:
        if not self._terminate_job(handle, exit_code):
            raise self._error('TerminateJobObject')

    def active_processes(self, handle: int) -> int:
        information = self._BasicAccountingInformation()
        if not self._query_information(
            handle,
            1,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
            None,
        ):
            raise self._error('QueryInformationJobObject')
        return int(information.ActiveProcesses)

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise self._error('CloseHandle')


class _WindowsJob:
    def __init__(self, *, api: Any, handle: int) -> None:
        self._api = api
        self._handle = handle
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._api.close(self._handle)
        self._closed = True

    def assign(self, process: subprocess.Popen[Any]) -> None:
        try:
            process_handle = getattr(process, '_handle', None)
            if process_handle is None:
                raise RuntimeError('subprocess does not expose its Windows process handle')
            self._api.assign(self._handle, int(process_handle))
        except BaseException:
            try:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                self.close()
            raise

    def terminate_and_wait(
        self,
        process: subprocess.Popen[Any],
        *,
        timeout: float,
    ) -> None:
        deadline = monotonic() + timeout
        try:
            self._api.terminate(self._handle, _WINDOWS_JOB_EXIT_CODE)
            while self._api.active_processes(self._handle):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError('Windows Job Object processes did not exit before timeout')
                sleep(min(0.01, remaining))
            remaining = max(0.0, deadline - monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Windows Job Object leader did not exit before timeout') from error
        finally:
            self.close()


def _new_windows_job() -> _WindowsJob:
    api = _WindowsJobApi()
    return _WindowsJob(api=api, handle=api.create_kill_on_close())


def _windows_bootstrap_command(command: Sequence[str]) -> list[str]:
    return [sys.executable, '-I', '-c', _WINDOWS_BOOTSTRAP, *command]


def _close_process_stdin(process: subprocess.Popen[Any]) -> None:
    stream = process.stdin
    process.stdin = None
    if stream is None:
        return
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        descriptor = None
    try:
        stream.close()
    except (OSError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _abort_windows_bootstrap(
    process: subprocess.Popen[Any],
    windows_job: _WindowsJob,
) -> None:
    _close_process_stdin(process)
    windows_job.terminate_and_wait(process, timeout=_WINDOWS_JOB_WAIT_TIMEOUT)


def _release_windows_bootstrap(
    process: subprocess.Popen[Any],
    windows_job: _WindowsJob,
) -> None:
    stream = process.stdin
    try:
        if stream is None:
            raise OSError('bootstrap release pipe is unavailable')
        written = os.write(stream.fileno(), _WINDOWS_BOOTSTRAP_RELEASE)
        if written != len(_WINDOWS_BOOTSTRAP_RELEASE):
            raise OSError('bootstrap release write was incomplete')
    except (AttributeError, OSError, ValueError) as error:
        try:
            _abort_windows_bootstrap(process, windows_job)
        except BaseException as cleanup_error:
            raise RuntimeError(
                'could not release Windows process bootstrap and cleanup failed'
            ) from cleanup_error
        raise RuntimeError('could not release Windows process bootstrap') from error
    _close_process_stdin(process)


def _start_owned_process(
    command: Sequence[str],
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], _WindowsJob | None]:
    if not _IS_WINDOWS:
        return subprocess.Popen(list(command), **popen_kwargs), None

    windows_job = _new_windows_job()
    try:
        process = subprocess.Popen(
            _windows_bootstrap_command(command),
            stdin=subprocess.PIPE,
            **popen_kwargs,
        )
    except BaseException as error:
        try:
            windows_job.close()
        except BaseException:
            pass
        if isinstance(error, OSError):
            raise OSError('could not start Windows process bootstrap') from None
        raise
    try:
        windows_job.assign(process)
    except BaseException:
        _close_process_stdin(process)
        raise
    _release_windows_bootstrap(process, windows_job)
    return process, windows_job


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


def _process_group_popen_kwargs(*, inherit_process_group: bool) -> dict[str, Any]:
    if _IS_WINDOWS:
        return {
            'creationflags': 0 if inherit_process_group else _CREATE_NEW_PROCESS_GROUP,
        }
    return {'start_new_session': not inherit_process_group}


def _stop_process(
    proc: subprocess.Popen[Any],
    *,
    windows_job: _WindowsJob | None = None,
    timeout: float = 5.0,
) -> None:
    if _IS_WINDOWS:
        if windows_job is None:
            raise RuntimeError('Windows subprocess has no owning Job Object')
        windows_job.terminate_and_wait(proc, timeout=timeout)
        return
    process_group_id = proc.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait(timeout=timeout)
        return
    if not _wait_for_group_exit(proc, process_group_id, timeout):
        try:
            os.killpg(process_group_id, _SIGKILL)
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
    proc, windows_job = _start_owned_process(
        command,
        cwd=args.cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        **_process_group_popen_kwargs(inherit_process_group=inherit_process_group),
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
        if _IS_WINDOWS:
            _stop_process(proc, windows_job=windows_job)
        elif inherit_process_group:
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
