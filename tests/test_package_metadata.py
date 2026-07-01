from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _section(name: str, text: str) -> str:
    match = re.search(rf'^\[{re.escape(name)}\]\n(?P<body>.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group('body')


def test_lsp_console_script_dependencies_are_installed_by_default() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    project = _section('project', pyproject)
    scripts = _section('project.scripts', pyproject)

    assert 'delphi-lsp = "delphiast.lsp_server:main"' in scripts
    assert '"pygls>=1.3.0,<2.0"' in project
    assert '"lsprotocol>=2023.0.1"' in project


def test_release_metadata_declares_1_0_1_and_windows_support() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    project = _section('project', pyproject)

    assert 'version = "1.0.1"' in project
    assert '"Operating System :: OS Independent"' in project
    assert '"Operating System :: Microsoft :: Windows"' in project
    assert '"Operating System :: MacOS"' in project
    assert '{ name = "Dark Light" }' in project
    assert 'F2011B' not in pyproject


def test_readme_parse_example_uses_public_api_signature() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')

    assert 'result = parse("unit Unit1; interface implementation end.", "Unit1.pas")' in readme


def test_sdist_includes_files_required_by_packaged_tests() -> None:
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')

    assert 'include opencode.json' in manifest
    assert 'include scripts/check_ornith_cache.py' in manifest
    assert 'include scripts/start_ornith_vllm.sh' in manifest
    assert 'include scripts/run_opencode_lsp_probe.py' in manifest
    assert 'include scripts/generate_release_evidence.py' in manifest
    assert 'include scripts/audit_delphi_language_features.py' in manifest
    assert 'include scripts/ollama/ornith-lspctx.Modelfile' in manifest
