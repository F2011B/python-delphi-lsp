import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_vllm_codebase_skill_test.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bootstrap_vllm_codebase_skill_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_writes_codebase_skill_sandbox_with_project_paths(tmp_path: Path) -> None:
    module = load_module()
    python_exe = tmp_path / ".venv" / "bin" / "python"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("#!/usr/bin/env python\n", encoding="utf-8")

    module.write_codebase_skill_sandbox(root=ROOT, sandbox=tmp_path, python_executable=python_exe)

    assert (tmp_path / "Main.dpr").exists()
    assert (tmp_path / "Main.dproj").exists()
    assert (tmp_path / "src" / "Mega100kUnit.pas").exists()
    assert (tmp_path / "include" / "build.inc").exists()
    assert (tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md").exists()
    assert (tmp_path / ".opencode" / "tools" / "delphi_codebase.ts").exists()
    config = (tmp_path / "opencode.json").read_text(encoding="utf-8")
    assert "vllm-delphi-codebase" in config
    assert '"delphi_codebase": true' in config
    assert '"skill": false' in config
    assert '"lsp": false' in config
    assert '"delphi_codebase": "allow"' in config
    assert '"skill": "deny"' in config
    assert '"temperature": 0' in config
    assert "When the user asks for a tool call" in config
    assert '"read": false' in config


def test_probe_command_requires_delphi_codebase_tool_and_forbids_raw_tools(tmp_path: Path) -> None:
    module = load_module()
    python_exe = tmp_path / ".venv" / "bin" / "python"
    command = module.build_probe_command(root=ROOT, python_executable=python_exe, sandbox=tmp_path, timeout=120)

    assert command[:2] == [str(python_exe), str(ROOT / "scripts" / "run_opencode_lsp_probe.py")]
    assert "vllm/ornith-lspctx" in command
    assert "vllm-delphi-codebase" in command
    assert "skill:delphi-codebase-navigator" not in command
    assert "delphi_codebase:MegaProc02500" in command
    assert "delphi_codebase:Value := Value + 40" in command
    assert "Do not write any explanatory text before calling that tool" in command[-1]
    assert "layer implementation" in command[-1]
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
