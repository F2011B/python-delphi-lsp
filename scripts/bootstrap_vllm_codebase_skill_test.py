#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delphi_lsp.agent_templates import install_opencode_support  # noqa: E402

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bootstrap_vllm_opencode_test import (  # noqa: E402
    DEFAULT_BASE_URL,
    ensure_venv,
    generated_mega_unit_source,
    require_opencode,
    run,
    start_vllm,
)


DEFAULT_SANDBOX = Path("output/mega_codebase_skill_project")
DEFAULT_MODEL = "vllm/ornith-lspctx"
DEFAULT_AGENT = "vllm-delphi-codebase"
DEFAULT_SYMBOL = "MegaProc02500"
DEFAULT_API_KEY = "vllm"
DEFAULT_MAX_MODEL_LEN = "32768"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def write_codebase_skill_sandbox(
    *,
    root: Path,
    sandbox: Path,
    python_executable: Path,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
) -> None:
    sandbox.mkdir(parents=True, exist_ok=True)
    write_text(
        sandbox / "Main.dpr",
        """
program Main;

uses
  Mega100kUnit in 'src/Mega100kUnit.pas';

begin
end.
""",
    )
    write_text(
        sandbox / "Main.dproj",
        """
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <MainSource>Main.dpr</MainSource>
    <DCC_UnitSearchPath>src</DCC_UnitSearchPath>
    <DCC_IncludePath>include</DCC_IncludePath>
    <DCC_Define>MSWINDOWS;CODEBASE_SKILL_PROBE</DCC_Define>
  </PropertyGroup>
  <ItemGroup>
    <DCCReference Include="src/Mega100kUnit.pas" />
  </ItemGroup>
</Project>
""",
    )
    write_text(sandbox / "include" / "build.inc", "const SkillProbeIncludedValue = 1;")
    source = generated_mega_unit_source().replace("interface\n\n", "interface\n{$I 'build.inc'}\n\n", 1)
    write_text(sandbox / "src" / "Mega100kUnit.pas", source)

    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    provider_options = config.setdefault("provider", {}).setdefault("vllm", {}).setdefault("options", {})
    provider_options["baseURL"] = base_url.rstrip("/")
    provider_options["apiKey"] = api_key
    config.setdefault("lsp", {}).setdefault("delphi", {})
    config["lsp"]["delphi"]["command"] = [str(python_executable), "-m", "delphi_lsp.lsp_server"]
    config["lsp"]["delphi"]["env"] = {"PYTHONPATH": str(root)}
    config["lsp"]["delphi"]["initialization"] = {"autoDiscoverPaths": True}
    (sandbox / "opencode.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    install_opencode_support(
        sandbox,
        python_executable=str(python_executable),
        force=True,
        write_config=True,
    )


def build_probe_command(
    *,
    root: Path,
    python_executable: Path,
    sandbox: Path,
    timeout: float,
    output: Path | None = None,
) -> list[str]:
    output_path = output or sandbox / "bootstrap_vllm_codebase_skill_probe.jsonl"
    prompt = (
        "First load the delphi-codebase-navigator skill. Use only delphi_codebase to inspect the Delphi project. "
        "Do not write explanatory text before the required calls. Call action open, then action find with query "
        '"MegaProc02500", focus the returned target with action focus and target_id, then call action inspect '
        "with detail body. Answer with path and line evidence, including `Value := Value + 40`."
    )
    return [
        str(python_executable),
        str(root / "scripts" / "run_opencode_lsp_probe.py"),
        "--cwd",
        str(sandbox),
        "--model",
        DEFAULT_MODEL,
        "--agent",
        DEFAULT_AGENT,
        "--require-tool",
        "delphi_codebase.open:Main.dpr",
        "--require-tool",
        f"delphi_codebase.find:{DEFAULT_SYMBOL}",
        "--require-tool",
        "delphi_codebase.focus:target_id",
        "--require-tool",
        "delphi_codebase.inspect:Value := Value + 40",
        "--require-tool",
        "skill:delphi-codebase-navigator",
        "--forbid-tool",
        "bash",
        "--forbid-tool",
        "read",
        "--forbid-tool",
        "glob",
        "--forbid-tool",
        "grep",
        "--forbid-tool",
        "edit",
        "--forbid-tool",
        "write",
        "--forbid-tool",
        "task",
        "--forbid-tool",
        "webfetch",
        "--forbid-tool",
        "todowrite",
        "--npm-cache",
        str(sandbox / ".opencode" / ".npm-cache"),
        "--timeout",
        str(timeout),
        "--output",
        str(output_path),
        prompt,
    ]


def endpoint_ready(base_url: str, *, api_key: str = DEFAULT_API_KEY) -> bool:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_endpoint(base_url: str, timeout: float, *, api_key: str = DEFAULT_API_KEY) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if endpoint_ready(base_url, api_key=api_key):
            return
        time.sleep(2)
    raise RuntimeError(f"vLLM endpoint did not become ready at {base_url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap and run the vLLM opencode Delphi codebase skill proof.")
    parser.add_argument("--sandbox", type=Path, default=ROOT / DEFAULT_SANDBOX)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--start-vllm", action="store_true", help="Start the local macOS vLLM helper before probing.")
    parser.add_argument("--use-running-server", action="store_true", help="Require an already running vLLM endpoint.")
    parser.add_argument("--allow-download", action="store_true", help="Permit Hugging Face downloads when starting vLLM.")
    parser.add_argument("--skip-install", action="store_true", help="Do not install package/dev dependencies into .venv.")
    parser.add_argument("--max-model-len", default=DEFAULT_MAX_MODEL_LEN)
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    parser.add_argument("--probe-timeout", type=float, default=180.0)
    args = parser.parse_args()

    python_executable = ensure_venv(ROOT, install=not args.skip_install)
    sandbox = args.sandbox.resolve()
    write_codebase_skill_sandbox(
        root=ROOT,
        sandbox=sandbox,
        python_executable=python_executable,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    print(f"Wrote {sandbox}")
    require_opencode()

    process: subprocess.Popen | None = None
    if not endpoint_ready(args.base_url, api_key=args.api_key):
        if not args.start_vllm:
            raise RuntimeError(
                f"vLLM endpoint is not reachable at {args.base_url}. "
                "Start it first or pass --start-vllm on macOS."
            )
        os.environ["MAX_MODEL_LEN"] = args.max_model_len
        process = start_vllm(
            ROOT,
            allow_download=args.allow_download,
            log_path=sandbox / "vllm_codebase_skill_bootstrap.log",
        )
    try:
        wait_for_endpoint(args.base_url, args.ready_timeout, api_key=args.api_key)
        env = os.environ.copy()
        env["OPENCODE_EXPERIMENTAL_LSP_TOOL"] = "true"
        npm_cache = sandbox / ".opencode" / ".npm-cache"
        bun_cache = sandbox / ".opencode" / ".bun-cache"
        npm_cache.mkdir(parents=True, exist_ok=True)
        bun_cache.mkdir(parents=True, exist_ok=True)
        env.setdefault("NPM_CONFIG_CACHE", str(npm_cache))
        env.setdefault("BUN_INSTALL_CACHE_DIR", str(bun_cache))
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


if __name__ == "__main__":
    raise SystemExit(main())
