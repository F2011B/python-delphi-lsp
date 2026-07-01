from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'bootstrap_vllm_opencode_test.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('bootstrap_vllm_opencode_test', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bootstrap_wrappers_are_documented_and_packaged() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')
    mac = (ROOT / 'scripts' / 'bootstrap_vllm_opencode_test.sh').read_text(encoding='utf-8')
    windows = (ROOT / 'scripts' / 'bootstrap_vllm_opencode_test.ps1').read_text(encoding='utf-8')

    assert 'scripts/bootstrap_vllm_opencode_test.sh --start-vllm' in readme
    assert '.\\scripts\\bootstrap_vllm_opencode_test.ps1 -UseRunningServer' in readme
    assert 'include scripts/bootstrap_vllm_opencode_test.py' in manifest
    assert 'include scripts/bootstrap_vllm_opencode_test.sh' in manifest
    assert 'include scripts/bootstrap_vllm_opencode_test.ps1' in manifest
    assert '"$ROOT_DIR/scripts/bootstrap_vllm_opencode_test.py" "$@"' in mac
    assert 'bootstrap_vllm_opencode_test.py' in windows


def test_generated_probe_unit_is_large_and_contains_expected_symbol() -> None:
    module = _load_module()

    source = module.generated_mega_unit_source()

    assert source.count('\n') > 100_000
    assert 'unit Mega100kUnit;' in source
    assert 'TMegaValue = Integer;' in source
    assert 'procedure MegaProc02500;' in source


def test_sandbox_config_uses_absolute_lsp_runtime(tmp_path) -> None:
    module = _load_module()
    python_exe = tmp_path / '.venv' / 'bin' / 'python'
    sandbox = tmp_path / 'output' / 'mega_lsp_chain_project'

    config_path = module.write_sandbox_config(
        root=ROOT,
        sandbox=sandbox,
        python_executable=python_exe,
    )

    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['lsp']['delphi']['command'] == [str(python_exe), '-m', 'delphiast.lsp_server']
    assert config['lsp']['delphi']['env']['PYTHONPATH'] == str(ROOT)
    assert config['agent']['vllm-lsp']['tools']['lsp'] is True
    assert config['agent']['vllm-lsp']['tools']['read'] is False


def test_probe_command_uses_lsp_only_vllm_agent(tmp_path) -> None:
    module = _load_module()
    python_exe = tmp_path / '.venv' / 'bin' / 'python'
    sandbox = tmp_path / 'output' / 'mega_lsp_chain_project'

    command = module.build_probe_command(
        root=ROOT,
        python_executable=python_exe,
        sandbox=sandbox,
        timeout=90.0,
    )

    assert command[:2] == [str(python_exe), str(ROOT / 'scripts' / 'run_opencode_lsp_probe.py')]
    assert '--model' in command
    assert 'vllm/ornith-lspctx' in command
    assert '--agent' in command
    assert 'vllm-lsp' in command
    assert '--require-tool' in command
    assert 'lsp.workspaceSymbol:MegaProc02500' in command
    assert '--forbid-tool' in command
    assert 'read' in command
