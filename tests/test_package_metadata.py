from __future__ import annotations

import json
import re
from pathlib import Path

import delphi_lsp
from delphi_lsp.lsp_server import create_server


ROOT = Path(__file__).resolve().parents[1]


def _section(name: str, text: str) -> str:
    match = re.search(rf'^\[{re.escape(name)}\]\n(?P<body>.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group('body')


def test_lsp_console_script_dependencies_are_installed_by_default() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    project = _section('project', pyproject)
    scripts = _section('project.scripts', pyproject)

    assert 'delphi-lsp = "delphi_lsp.lsp_server:main"' in scripts
    assert 'delphi-lsp-agent = "delphi_lsp.agent_cli:main"' in scripts
    assert '"pygls>=1.3.0,<2.0"' in project
    assert '"lsprotocol>=2023.0.1"' in project


def test_release_metadata_declares_3_1_0_sole_namespace_author_and_windows_support() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    project = _section('project', pyproject)
    scripts = _section('project.scripts', pyproject)

    assert 'version = "3.1.0"' in project
    assert '"Operating System :: OS Independent"' in project
    assert '"Operating System :: Microsoft :: Windows"' in project
    assert '"Operating System :: MacOS"' in project
    assert '"Development Status :: 5 - Production/Stable"' in project
    assert '{ name = "Dark Light" }' in project
    assert pyproject.count('{ name = "Dark Light" }') == 1
    assert 'packages = ["delphi_lsp"]' in pyproject
    assert 'delphi-lsp = "delphi_lsp.lsp_server:main"' in scripts
    assert 'delphi-lsp-agent = "delphi_lsp.agent_cli:main"' in scripts
    assert 'F2011B' not in pyproject


def test_public_and_lsp_versions_match_release_metadata() -> None:
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    match = re.search(r'^version = "([^"]+)"$', _section('project', pyproject), re.MULTILINE)
    assert match is not None
    version = match.group(1)

    assert delphi_lsp.__version__ == version
    assert create_server().version == version


def test_readme_documents_v3_release_plugin_protocol_discovery_and_vllm_proof() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    forbidden = 'delphi' + 'ast'

    assert 'result = parse("unit Unit1; interface implementation end.", "Unit1.pas")' in readme
    assert 'from delphi_lsp import parse' in readme
    assert 'build_workspace_semantics' in readme
    assert 'ProjectIndexer' in readme
    assert '["delphi-lsp"]' in readme
    assert 'python -m delphi_lsp.lsp_server' in readme
    assert '"autoDiscoverPaths": true' in readme
    assert '.agents/skills/python-delphi-lsp/SKILL.md' in readme
    assert '.opencode/agents/python-delphi-lsp.md' in readme
    assert '.opencode/plugins/delphi_codebase.ts' in readme
    assert 'Protocol v3' in readme
    assert 'sound_partial' in readme
    assert 'workspace_revision' in readme
    assert '117,511-line' in readme
    assert 'MegaProc02500' in readme
    assert 'Value := Value + 40' in readme
    assert 'Windows' in readme
    assert 'macOS-only' in readme
    assert forbidden not in readme.casefold()
    assert 'delphi-lsp-agent opencode install --target .' in readme
    assert 'Auto-discovery reads `.dpr`, `.dpk`, `.dproj`, `.cfg`, and `.dof` files' in readme
    assert 'no file-size threshold' in readme
    assert 'final answer' in readme
    assert 'Version 3.1.0' in readme
    assert 'delphi-lsp-agent wiki export --root PATH' in readme
    assert 'Open Knowledge Format 0.2 specification' in readme
    assert '`cpg`' in readme
    assert '`ast`, `cfg`, `dfg`, `call`, and `full`' in readme
    assert '`graph`, `direction`, and `depth`' in readme
    assert '20 percent' in readme
    assert '128 MiB' in readme
    assert '4,021,192' in readme
    assert '22.62' in readme
    assert '0.0166' in readme
    assert '0.000069' in readme
    assert '98.43 percent' in readme
    assert 'view --layer metrics' in readme
    assert '`metrics`' in readme
    assert 'total_loc_with_includes' in readme
    assert 'Cyclomatic complexity' in readme
    assert 'Halstead' in readme
    assert 'maintainability index' in readme
    assert 'afferent coupling' in readme
    assert 'efferent coupling' in readme
    assert 'instability' in readme
    assert 'abstractness' in readme
    assert 'distance from the main sequence' in readme
    assert '--probe metrics' in readme
    assert '`.delphi-lsp.toml`' in readme
    assert '[projects]' in readme
    assert 'include = [' in readme
    assert 'exclude = [' in readme
    assert 'exclude` always wins' in readme
    assert 'matched no Delphi projects' in readme
    assert '[workspace]' in readme
    assert 'never traversed or parsed' in readme
    assert '`projects.exclude`' in readme
    assert '`workspace.exclude`' in readme


def test_root_opencode_config_is_portable_and_has_no_pythonpath_requirement() -> None:
    config_text = (ROOT / 'opencode.json').read_text(encoding='utf-8')
    config = json.loads(config_text)
    forbidden = 'delphi' + 'ast'

    assert config['lsp']['delphi']['command'] == ['delphi-lsp']
    assert 'env' not in config['lsp']['delphi']
    assert 'PYTHONPATH' not in config_text
    assert forbidden not in config_text.casefold()


def test_ci_covers_cross_platform_test_matrix_and_release_build() -> None:
    workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')

    assert 'ubuntu-latest' in workflow
    assert 'macos-latest' in workflow
    assert 'windows-latest' in workflow
    assert '"3.10"' in workflow
    assert '"3.14"' in workflow
    assert 'cache: pip' in workflow
    assert 'pip install .[test]' in workflow
    assert 'pytest' in workflow
    assert 'python -m build' in workflow
    assert 'python -m twine check dist/*' in workflow
    assert 'pip install dist/*.whl' in workflow


def test_sdist_includes_files_required_by_packaged_tests() -> None:
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')

    assert 'include .github/workflows/ci.yml' in manifest
    assert 'include opencode.json' in manifest
    assert 'include scripts/check_ornith_cache.py' in manifest
    assert 'include scripts/start_ornith_vllm.sh' in manifest
    assert 'include scripts/run_opencode_lsp_probe.py' in manifest
    assert 'include scripts/run_openrouter_github_e2e.py' in manifest
    assert 'include scripts/bootstrap_vllm_codebase_skill_test.py' in manifest
    assert 'include scripts/generate_release_evidence.py' in manifest
    assert 'include scripts/generate_progress_pdf.py' in manifest
    assert 'include scripts/audit_delphi_language_features.py' in manifest
    assert 'include scripts/build_github_performance_corpus.py' in manifest
    assert 'include scripts/benchmark_github_corpus.py' in manifest
    assert 'include scripts/benchmark_cpg.py' in manifest
    assert 'include scripts/benchmark_parallel_cache.py' in manifest
    assert 'include scripts/ollama/ornith-lspctx.Modelfile' in manifest
    assert 'include tests/corpora.performance.lock.json' in manifest


def test_release_3_0_0_notes_document_protocol_cpg_and_performance_gate() -> None:
    notes = (ROOT / 'docs' / 'release-3.0.0.md').read_text(encoding='utf-8')

    assert '# Python Delphi LSP 3.0.0' in notes
    assert 'Protocol v3' in notes
    assert '`cpg`' in notes
    assert 'sound_partial' in notes
    assert 'target IDs remain unchanged' in notes
    assert '4,021,192' in notes
    assert '22.62' in notes
    assert '98.43 percent' in notes
    assert 'one source' in notes


def test_release_3_1_0_notes_document_okf_export_and_symbols_fix() -> None:
    notes = (ROOT / 'docs' / 'release-3.1.0.md').read_text(encoding='utf-8')

    assert '# Python Delphi LSP 3.1.0' in notes
    assert 'Open Knowledge Format 0.2' in notes
    assert 'wiki export' in notes
    assert 'all symbols' in notes
    assert 'PyPI' in notes
