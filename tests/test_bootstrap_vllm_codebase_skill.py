import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_vllm_codebase_skill_test.py"


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def load_module():
    spec = importlib.util.spec_from_file_location("bootstrap_vllm_codebase_skill_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_writes_codebase_skill_sandbox_with_project_paths(tmp_path: Path) -> None:
    module = load_module()
    python_exe = venv_python(tmp_path)
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("#!/usr/bin/env python\n", encoding="utf-8")

    module.write_codebase_skill_sandbox(root=ROOT, sandbox=tmp_path, python_executable=python_exe)

    assert (tmp_path / "Main.dpr").exists()
    assert (tmp_path / "Main.dproj").exists()
    assert (tmp_path / "src" / "Mega100kUnit.pas").exists()
    assert (tmp_path / "include" / "build.inc").exists()
    assert (tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md").exists()
    assert (tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts").exists()
    assert not (tmp_path / ".opencode" / "tools" / "delphi_codebase.ts").exists()
    config = (tmp_path / "opencode.json").read_text(encoding="utf-8")
    assert "vllm-delphi-codebase" in config
    assert '"delphi_codebase": true' in config
    assert '"skill": true' in config
    assert '"lsp": false' in config
    assert '"delphi_codebase": "allow"' in config
    assert '"skill": {' in config
    assert '"*": "deny"' in config
    assert '"delphi-codebase-navigator": "allow"' in config
    assert '"temperature": 0' in config
    assert "load delphi-codebase-navigator first" in config
    assert '"read": false' in config
    parsed_config = json.loads(config)
    assert "env" not in parsed_config["lsp"]["delphi"]
    assert "PYTHONPATH" not in config
    provider_options = parsed_config["provider"]["vllm"]["options"]
    assert provider_options["baseURL"] == module.DEFAULT_BASE_URL
    assert provider_options["apiKey"] == module.DEFAULT_API_KEY


def test_written_sandbox_mega_unit_has_documented_line_count(tmp_path: Path) -> None:
    module = load_module()

    module.write_codebase_skill_sandbox(
        root=ROOT,
        sandbox=tmp_path,
        python_executable=venv_python(tmp_path),
    )

    source = (tmp_path / "src" / "Mega100kUnit.pas").read_text(encoding="utf-8")
    assert len(source.splitlines()) == 117_511


def test_script_help_runs_outside_checkout_without_pythonpath(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--base-url" in result.stdout
    script = SCRIPT.read_text(encoding="utf-8")
    root_bootstrap = "sys.path.insert(0, str(ROOT))"
    agent_import = "from delphi_lsp.agent_templates import install_opencode_support"
    assert root_bootstrap in script
    assert script.index(root_bootstrap) < script.index(agent_import)


def test_writes_requested_vllm_provider_config(tmp_path: Path) -> None:
    module = load_module()
    python_exe = venv_python(tmp_path)

    module.write_codebase_skill_sandbox(
        root=ROOT,
        sandbox=tmp_path,
        python_executable=python_exe,
        base_url="http://127.0.0.1:9000/v1/",
        api_key="custom-vllm-key",
    )

    config = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    provider_options = config["provider"]["vllm"]["options"]
    assert provider_options["baseURL"] == "http://127.0.0.1:9000/v1"
    assert provider_options["apiKey"] == "custom-vllm-key"


def test_probe_command_requires_delphi_codebase_tool_and_forbids_raw_tools(tmp_path: Path) -> None:
    module = load_module()
    python_exe = venv_python(tmp_path)
    command = module.build_probe_command(root=ROOT, python_executable=python_exe, sandbox=tmp_path, timeout=120)

    assert command[:2] == [str(python_exe), str(ROOT / "scripts" / "run_opencode_lsp_probe.py")]
    assert "vllm/ornith-lspctx" in command
    assert "vllm-delphi-codebase" in command
    assert "skill:delphi-codebase-navigator" in command
    assert "delphi_codebase.open:Main.dpr" in command
    assert "delphi_codebase.find:MegaProc02500" in command
    assert "delphi_codebase.focus:target_id" in command
    assert "delphi_codebase.inspect:Value := Value + 40" in command
    required_final = [command[index + 1] for index, item in enumerate(command) if item == "--require-final"]
    assert {"src/Mega100kUnit.pas", "117464", "Value := Value + 40"}.issubset(set(required_final))
    assert "load the delphi-codebase-navigator skill" in command[-1]
    assert "action open" in command[-1]
    assert "action find" in command[-1]
    assert "action focus" in command[-1]
    assert "action inspect" in command[-1]
    assert "--npm-cache" in command
    assert command[command.index("--npm-cache") + 1] == str(tmp_path / ".opencode" / ".npm-cache")
    forbidden = [command[index + 1] for index, item in enumerate(command) if item == "--forbid-tool"]
    assert {"bash", "read", "glob", "grep", "edit", "write", "task", "webfetch", "todowrite"}.issubset(
        set(forbidden)
    )


def test_skill_bootstrap_defaults_to_smaller_vllm_context_for_metal_stability() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'DEFAULT_MAX_MODEL_LEN = "32768"' in script
    assert 'os.environ["MAX_MODEL_LEN"] = args.max_model_len' in script
    assert 'env.setdefault("NPM_CONFIG_CACHE"' in script
    assert 'env.setdefault("BUN_INSTALL_CACHE_DIR"' in script
