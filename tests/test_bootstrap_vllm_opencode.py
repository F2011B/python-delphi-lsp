from __future__ import annotations

import importlib.util
import json
import os
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


def _venv_python(root: Path) -> Path:
    if os.name == 'nt':
        return root / '.venv' / 'Scripts' / 'python.exe'
    return root / '.venv' / 'bin' / 'python'


def test_readme_uses_semantic_skill_bootstrap_for_primary_large_project_proof() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    proof = readme.split('## Reproducible large-project vLLM proof', 1)[1].split('## Migration to 2.0', 1)[0]
    commands = {
        line.strip()
        for line in proof.splitlines()
        if 'bootstrap_vllm_codebase_skill_test.py' in line
    }

    assert 'python scripts/bootstrap_vllm_codebase_skill_test.py --use-running-server' in commands
    assert 'python scripts/bootstrap_vllm_codebase_skill_test.py --start-vllm' in commands
    assert 'python .\\scripts\\bootstrap_vllm_codebase_skill_test.py --use-running-server' in commands
    assert (
        'python .\\scripts\\bootstrap_vllm_codebase_skill_test.py '
        '--use-running-server --base-url http://127.0.0.1:9000/v1'
    ) in commands
    assert all('--skip-install' not in command for command in commands)
    assert '`--skip-install` is an optional acceleration' in proof
    assert 'already prepared `.venv`' in proof
    assert (
        'The verifier requires `skill`, `open` (`Main.dpr` evidence), `find`, `focus`, and `inspect`'
    ) in proof
    assert 'scripts/bootstrap_vllm_opencode_test.sh' not in proof
    assert 'bootstrap_vllm_opencode_test.ps1' not in proof


def test_readme_checkout_verification_installs_dev_dependencies_before_packaging() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    verification = readme.split('## Verification and limitations', 1)[1].split('## License', 1)[0]

    install = 'python -m pip install -e ".[dev]"'
    assert install in verification
    assert verification.index(install) < verification.index('python -m build')
    assert verification.index(install) < verification.index('python -m twine check dist/*')


def test_raw_lsp_bootstrap_wrappers_are_packaged() -> None:
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')
    mac = (ROOT / 'scripts' / 'bootstrap_vllm_opencode_test.sh').read_text(encoding='utf-8')
    windows = (ROOT / 'scripts' / 'bootstrap_vllm_opencode_test.ps1').read_text(encoding='utf-8')

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


def test_raw_lsp_sandbox_config_uses_absolute_runtime_with_explicit_env_map(tmp_path) -> None:
    module = _load_module()
    python_exe = _venv_python(tmp_path)
    sandbox = tmp_path / 'output' / 'mega_lsp_chain_project'
    fixture_root = tmp_path / 'fixture-root'
    fixture_root.mkdir()
    fixture_config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))
    fixture_config['lsp']['delphi']['env'] = {}
    (fixture_root / 'opencode.json').write_text(json.dumps(fixture_config), encoding='utf-8')

    config_path = module.write_sandbox_config(
        root=fixture_root,
        sandbox=sandbox,
        python_executable=python_exe,
    )

    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['lsp']['delphi']['command'] == [str(python_exe), '-m', 'delphi_lsp.lsp_server']
    assert config['lsp']['delphi']['env']['PYTHONPATH'] == str(fixture_root)
    assert config['agent']['vllm-lsp']['tools']['lsp'] is True
    assert config['agent']['vllm-lsp']['tools']['read'] is False


def test_probe_command_uses_lsp_only_vllm_agent(tmp_path) -> None:
    module = _load_module()
    python_exe = _venv_python(tmp_path)
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
