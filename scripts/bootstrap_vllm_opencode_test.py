#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SANDBOX = Path('output/mega_lsp_chain_project')
DEFAULT_BASE_URL = 'http://127.0.0.1:8001/v1'
DEFAULT_MODEL = 'vllm/ornith-lspctx'
DEFAULT_AGENT = 'vllm-lsp'
DEFAULT_SYMBOL = 'MegaProc02500'


def generated_mega_unit_source(proc_count: int = 2500, statements_per_proc: int = 40) -> str:
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


def venv_python(root: Path) -> Path:
    scripts_dir = 'Scripts' if platform.system() == 'Windows' else 'bin'
    executable = 'python.exe' if platform.system() == 'Windows' else 'python'
    return root / '.venv' / scripts_dir / executable


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print('+ ' + ' '.join(command))
    subprocess.run(command, cwd=cwd, env=env, check=True)


def ensure_venv(root: Path, *, install: bool) -> Path:
    python_executable = venv_python(root)
    if not python_executable.exists():
        base_python = shutil.which('python3') or shutil.which('python') or sys.executable
        run([base_python, '-m', 'venv', str(root / '.venv')], cwd=root)
    if install:
        run([str(python_executable), '-m', 'pip', 'install', '-U', 'pip'], cwd=root)
        run([str(python_executable), '-m', 'pip', 'install', '-e', '.[dev]'], cwd=root)
    return python_executable


def write_mega_unit(sandbox: Path) -> Path:
    sandbox.mkdir(parents=True, exist_ok=True)
    target = sandbox / 'Mega100kUnit.pas'
    target.write_text(generated_mega_unit_source(), encoding='utf-8')
    return target


def write_sandbox_config(*, root: Path, sandbox: Path, python_executable: Path) -> Path:
    sandbox.mkdir(parents=True, exist_ok=True)
    config = json.loads((root / 'opencode.json').read_text(encoding='utf-8'))
    config['lsp']['delphi']['command'] = [str(python_executable), '-m', 'delphiast.lsp_server']
    config['lsp']['delphi']['env']['PYTHONPATH'] = str(root)
    config['lsp']['delphi']['initialization']['includePaths'] = [
        str(root / 'tests' / 'fixtures'),
        str(root / 'tests' / 'fixtures' / 'legacy_snippets'),
    ]
    target = sandbox / 'opencode.json'
    target.write_text(json.dumps(config, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return target


def endpoint_ready(base_url: str) -> bool:
    request = urllib.request.Request(f'{base_url.rstrip("/")}/models')
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_endpoint(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if endpoint_ready(base_url):
            return
        time.sleep(2)
    raise RuntimeError(f'vLLM endpoint did not become ready at {base_url}')


def start_vllm(root: Path, *, allow_download: bool, log_path: Path) -> subprocess.Popen:
    if platform.system() == 'Windows':
        raise RuntimeError(
            'Automatic vLLM startup is not supported by this wrapper on Windows. '
            'Start a vLLM-compatible endpoint first and rerun with -UseRunningServer.'
        )
    command = [str(root / 'scripts' / 'start_ornith_vllm.sh')]
    if allow_download:
        command.append('--allow-download')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open('w', encoding='utf-8')
    print('+ ' + ' '.join(command))
    return subprocess.Popen(command, cwd=root, stdout=log_file, stderr=subprocess.STDOUT, text=True)


def build_probe_command(
    *,
    root: Path,
    python_executable: Path,
    sandbox: Path,
    timeout: float,
    output: Path | None = None,
) -> list[str]:
    output_path = output or sandbox / 'bootstrap_vllm_lsp_probe.jsonl'
    prompt = (
        'Use only the Delphi LSP tool. In file Mega100kUnit.pas, run workspaceSymbol '
        'with filePath "Mega100kUnit.pas", line 1, character 1, and query "MegaProc02500".'
    )
    return [
        str(python_executable),
        str(root / 'scripts' / 'run_opencode_lsp_probe.py'),
        '--cwd',
        str(sandbox),
        '--model',
        DEFAULT_MODEL,
        '--agent',
        DEFAULT_AGENT,
        '--require-tool',
        f'lsp.workspaceSymbol:{DEFAULT_SYMBOL}',
        '--forbid-tool',
        'bash',
        '--forbid-tool',
        'read',
        '--forbid-tool',
        'glob',
        '--forbid-tool',
        'grep',
        '--forbid-tool',
        'edit',
        '--forbid-tool',
        'write',
        '--forbid-tool',
        'task',
        '--forbid-tool',
        'webfetch',
        '--forbid-tool',
        'todowrite',
        '--forbid-tool',
        'skill',
        '--timeout',
        str(timeout),
        '--output',
        str(output_path),
        prompt,
    ]


def require_opencode() -> None:
    if shutil.which('opencode') is None:
        raise RuntimeError('opencode was not found on PATH')


def main() -> int:
    parser = argparse.ArgumentParser(description='Bootstrap and run the vLLM opencode Delphi LSP proof.')
    parser.add_argument('--sandbox', type=Path, default=ROOT / DEFAULT_SANDBOX)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--start-vllm', action='store_true', help='Start the local macOS vLLM helper before probing.')
    parser.add_argument('--use-running-server', action='store_true', help='Require an already running vLLM endpoint.')
    parser.add_argument('--allow-download', action='store_true', help='Permit Hugging Face downloads when starting vLLM.')
    parser.add_argument('--skip-install', action='store_true', help='Do not install package/dev dependencies into .venv.')
    parser.add_argument('--ready-timeout', type=float, default=180.0)
    parser.add_argument('--probe-timeout', type=float, default=90.0)
    args = parser.parse_args()

    python_executable = ensure_venv(ROOT, install=not args.skip_install)
    sandbox = args.sandbox.resolve()
    mega_unit = write_mega_unit(sandbox)
    config = write_sandbox_config(root=ROOT, sandbox=sandbox, python_executable=python_executable.resolve())
    print(f'Wrote {mega_unit}')
    print(f'Wrote {config}')
    require_opencode()

    process: subprocess.Popen | None = None
    if not endpoint_ready(args.base_url):
        if not args.start_vllm:
            raise RuntimeError(
                f'vLLM endpoint is not reachable at {args.base_url}. '
                'Start it first or pass --start-vllm on macOS.'
            )
        process = start_vllm(
            ROOT,
            allow_download=args.allow_download,
            log_path=sandbox / 'vllm_bootstrap.log',
        )
    try:
        wait_for_endpoint(args.base_url, args.ready_timeout)
        env = os.environ.copy()
        env['OPENCODE_EXPERIMENTAL_LSP_TOOL'] = 'true'
        run(
            build_probe_command(
                root=ROOT,
                python_executable=python_executable,
                sandbox=sandbox,
                timeout=args.probe_timeout,
            ),
            cwd=ROOT,
            env=env,
        )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
