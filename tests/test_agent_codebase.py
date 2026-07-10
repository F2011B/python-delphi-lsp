import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from delphi_lsp.agent_layers import build_codebase_index, render_layer
from delphi_lsp.project_indexer import ProjectIndexer


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def make_project(root: Path) -> None:
    write_text(
        root / "Main.dpr",
        """
        program Main;

        uses
          Worker in 'src/Worker.pas';

        begin
        end.
        """,
    )
    write_text(
        root / "src" / "Worker.pas",
        """
        unit Worker;

        interface

        type
          TWorker = class
          public
            procedure Run;
          end;

        implementation

        procedure TWorker.Run;
        begin
          Writeln('body must not be exposed in the layer output');
        end;

        end.
        """,
    )


def make_mega_project(root: Path, *, proc_count: int = 2500, statements_per_proc: int = 40) -> None:
    lines = [
        "unit Mega100kUnit;",
        "",
        "interface",
        "",
        "type",
        "  TMegaValue = Integer;",
        "",
        "implementation",
        "",
    ]
    for index in range(1, proc_count + 1):
        lines.append(f"procedure MegaProc{index:05d};")
        lines.append("var")
        lines.append("  Value: Integer;")
        lines.append("begin")
        lines.append("  Value := 0;")
        for statement in range(1, statements_per_proc + 1):
            lines.append(f"  Value := Value + {statement};")
        lines.append("end;")
        lines.append("")
    lines.append("end.")
    write_text(root / "Mega100kUnit.pas", "\n".join(lines) + "\n")


def test_builds_layered_markdown_without_exposing_routine_bodies(tmp_path: Path) -> None:
    make_project(tmp_path)

    index = build_codebase_index(tmp_path)
    markdown = render_layer(index, "unit", query="Worker", output_format="markdown")

    assert "Worker.pas" in markdown
    assert "TWorker" in markdown
    assert "Run" in markdown
    assert "body must not be exposed" not in markdown


def test_implementation_layer_exposes_only_queried_method_body(tmp_path: Path) -> None:
    make_project(tmp_path)

    index = build_codebase_index(tmp_path)
    markdown = render_layer(index, "implementation", query="TWorker.Run", output_format="markdown")

    assert "TWorker.Run" in markdown
    assert "procedure TWorker.Run;" in markdown
    assert "body must not be exposed in the layer output" in markdown
    assert "unit Worker;" not in markdown


def test_implementation_layer_exposes_class_declaration_and_method_bodies(tmp_path: Path) -> None:
    make_project(tmp_path)

    index = build_codebase_index(tmp_path)
    payload = json.loads(render_layer(index, "implementation", query="TWorker", output_format="json"))

    assert payload["layer"] == "implementation"
    [item] = payload["items"]
    fragment_kinds = {fragment["fragment_kind"] for fragment in item["fragments"]}
    fragment_text = "\n".join(fragment["text"] for fragment in item["fragments"])
    assert fragment_kinds == {"declaration", "implementation"}
    assert "TWorker = class" in fragment_text
    assert "procedure TWorker.Run;" in fragment_text
    assert "body must not be exposed in the layer output" in fragment_text


def test_implementation_layer_slices_single_method_from_100k_line_file(tmp_path: Path) -> None:
    make_mega_project(tmp_path)
    source = (tmp_path / "Mega100kUnit.pas").read_text(encoding="utf-8")
    assert source.count("\n") > 100_000

    index = build_codebase_index(tmp_path)
    payload = json.loads(render_layer(index, "implementation", query="MegaProc02500", output_format="json"))

    [item] = payload["items"]
    text = item["fragments"][0]["text"]
    assert item["name"] == "MegaProc02500"
    assert "procedure MegaProc02500;" in text
    assert "  Value := Value + 40;" in text
    assert "procedure MegaProc02499;" not in text
    assert "procedure MegaProc02498;" not in text
    assert item["fragments"][0]["range"]["start_line"] > 100_000


def test_default_layer_index_does_not_deep_parse_project_dependencies(tmp_path: Path, monkeypatch) -> None:
    make_project(tmp_path)

    def fail_if_called(self, file_name: str):  # noqa: ANN001
        raise AssertionError(f"unexpected deep project parse for {file_name}")

    monkeypatch.setattr(ProjectIndexer, "index", fail_if_called)

    index = build_codebase_index(tmp_path)
    overview = render_layer(index, "overview")
    symbols = render_layer(index, "symbols", query="TWorker")

    assert "Sources:" in overview
    assert "TWorker" in symbols


def test_agent_cli_outputs_symbol_layer_as_json(tmp_path: Path) -> None:
    make_project(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "delphi_lsp.agent_cli",
            "view",
            "--root",
            str(tmp_path),
            "--layer",
            "implementation",
            "--query",
            "TWorker.Run",
            "--format",
            "json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["layer"] == "implementation"
    assert any(item["name"] == "TWorker.Run" for item in payload["items"])
    assert "procedure TWorker.Run;" in completed.stdout


def test_opencode_install_writes_protocol_v2_skill_plugin_and_config(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "delphi_lsp.agent_cli",
            "opencode",
            "install",
            "--target",
            str(tmp_path),
            "--python",
            sys.executable,
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    skill = tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md"
    plugin = tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts"
    assert skill.exists()
    assert plugin.exists()
    assert not (tmp_path / ".opencode" / "tools" / "delphi_codebase.ts").exists()
    skill_text = skill.read_text(encoding="utf-8")
    assert "name: delphi-codebase-navigator" in skill_text
    assert "only through `delphi_codebase`" in skill_text
    assert "never raw bash/read/glob/grep/cat/shell" in skill_text
    assert "sound_partial" in skill_text
    plugin_text = plugin.read_text(encoding="utf-8")
    assert sys.executable in plugin_text
    assert "delphi_lsp.agent_cli" in plugin_text
    assert '@opencode-ai/plugin' in plugin_text
    assert "type Plugin" in plugin_text
    assert "Bun.spawn(command" in plugin_text
    assert "Bun.$" not in plugin_text
    assert '"worker"' in plugin_text
    assert "sessionID" in plugin_text
    assert "AbortSignal" in plugin_text
    assert "session.deleted" in plugin_text
    assert "experimental.session.compacting" in plugin_text
    assert 'action: "focus"' in plugin_text
    assert 'action: "inspect", detail: "summary"' in plugin_text
    assert "windowsHide: true" in plugin_text
    assert "root: tool.schema" not in plugin_text
    assert "max_items" in plugin_text
    assert "max_chars" in plugin_text
    assert "WorkerProtocolError" in plugin_text
    assert "error instanceof WorkerProtocolError" in plugin_text
    assert "console.log" not in plugin_text
    assert "stderr" in plugin_text
    assert str(skill) in completed.stdout


def test_generated_plugin_uses_official_compaction_hook_separately_from_event(tmp_path: Path) -> None:
    from delphi_lsp.agent_templates import install_opencode_support

    _, plugin, _ = install_opencode_support(tmp_path, python_executable=sys.executable)
    plugin_text = plugin.read_text(encoding="utf-8")

    assert '"experimental.session.compacting": async ({ sessionID }, output) => {' in plugin_text
    assert "output.context.push(" in plugin_text
    assert 'event: async ({ event }) => {' in plugin_text
    assert 'event.type === "experimental.session.compacting"' not in plugin_text
    assert 'event.type !== "experimental.session.compacting"' not in plugin_text


def test_generated_plugin_closes_every_worker_for_deleted_session(tmp_path: Path) -> None:
    from delphi_lsp.agent_templates import install_opencode_support

    _, plugin, _ = install_opencode_support(tmp_path, python_executable=sys.executable)
    plugin_text = plugin.read_text(encoding="utf-8")

    assert 'if (event.type !== "session.deleted") return' in plugin_text
    assert "closeSession(event.properties.info.id)" in plugin_text
    assert "for (const client of clients.values()) client.close()" in plugin_text


def test_generated_plugin_flushes_and_has_one_named_export_without_nul(tmp_path: Path) -> None:
    from delphi_lsp.agent_templates import install_opencode_support

    _, plugin, _ = install_opencode_support(tmp_path, python_executable=sys.executable)
    plugin_bytes = plugin.read_bytes()
    plugin_text = plugin_bytes.decode("utf-8")

    assert "await this.proc.stdin.flush()" in plugin_text
    assert plugin_text.count("export const DelphiCodebasePlugin: Plugin = async") == 1
    assert "export const DelphiCodebasePlugin: Plugin = async (_input) => {" in plugin_text
    assert "async (_input, options)" not in plugin_text
    assert "const DEFAULT_REQUEST_TIMEOUT_MS = 120_000" in plugin_text
    assert "export default" not in plugin_text
    assert b"\x00" not in plugin_bytes
    directory_guard = 'if (context.directory && context.directory !== "/") return context.directory'
    worktree_guard = 'if (context.worktree && context.worktree !== "/") return context.worktree'
    assert directory_guard in plugin_text
    assert worktree_guard in plugin_text
    assert plugin_text.index(directory_guard) < plugin_text.index(worktree_guard)


def test_generated_plugin_classifies_protocol_and_transport_failures(tmp_path: Path) -> None:
    from delphi_lsp.agent_templates import install_opencode_support

    _, plugin, _ = install_opencode_support(tmp_path, python_executable=sys.executable)
    plugin_text = plugin.read_text(encoding="utf-8")

    assert "class WorkerProtocolError extends Error" in plugin_text
    assert "class WorkerTransportError extends Error" in plugin_text
    assert 'new WorkerProtocolError("delphi_codebase returned malformed JSON.")' in plugin_text
    assert 'new WorkerProtocolError("delphi_codebase returned an incomplete response.", false)' in plugin_text
    assert 'new WorkerTransportError("delphi_codebase worker closed without a response.")' in plugin_text
    assert "error instanceof WorkerProtocolError && error.reusable" in plugin_text


def test_generated_plugin_runtime_reuses_and_cleans_workers_without_bun(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the generated plugin runtime harness")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True).stdout.strip()
    major = int(version.removeprefix("v").split(".", 1)[0])
    if major < 22:
        pytest.skip("Node 22+ is required for TypeScript type stripping")

    from delphi_lsp.agent_templates import install_opencode_support

    _, plugin, _ = install_opencode_support(tmp_path, python_executable=sys.executable)
    plugin_text = plugin.read_text(encoding="utf-8")
    assert "await this.proc.stdin.flush()" in plugin_text
    plugin.write_text(
        plugin_text.replace(
            "const DEFAULT_REQUEST_TIMEOUT_MS = 120_000",
            "const DEFAULT_REQUEST_TIMEOUT_MS = 25",
            1,
        ),
        encoding="utf-8",
    )

    write_text(
        tmp_path / "node_modules" / "@opencode-ai" / "plugin" / "package.json",
        """
        {"name":"@opencode-ai/plugin","type":"module","exports":"./index.js"}
        """,
    )
    write_text(
        tmp_path / "node_modules" / "@opencode-ai" / "plugin" / "index.js",
        """
        const chain = new Proxy(() => chain, {
          get: () => (..._args) => chain,
        })
        export const tool = Object.assign((definition) => definition, {
          schema: {
            enum: () => chain,
            string: () => chain,
            number: () => chain,
          },
        })
        """,
    )
    write_text(
        tmp_path / "runtime_harness.mjs",
        """
        import assert from "node:assert/strict"

        const encoder = new TextEncoder()
        const processes = []
        const plans = new Map()
        const unhandledRejections = []
        process.on("unhandledRejection", (error) => unhandledRejections.push(error))

        function plan(root, ...behaviors) {
          plans.set(root, behaviors)
        }

        function nextBehavior(root) {
          const behaviors = plans.get(root)
          return behaviors?.shift() ?? "success"
        }

        function responseFor(request) {
          const focus = request.action === "focus"
            ? { project_id: "project", unit_id: "unit", target_id: "target_v2_1" }
            : { project_id: "project", unit_id: "unit", target_id: "" }
          const result = request.action === "inspect"
            ? [{ path: "src/Ünit.pas", line: 7, name: "Grüße" }]
            : [{ name: "Grüße" }]
          return JSON.stringify({
            schema: 2,
            workspace_revision: "revision-1",
            focus,
            result,
            page: { returned: 1, total: 1, truncated: false, next_cursor: "" },
            context: { chars: 20, approx_tokens: 5 },
          }) + "\\n"
        }

        globalThis.Bun = {
          spawn(command, options) {
            const root = command.at(-1)
            let stdoutController
            let stderrController
            let pending = ""
            let stdoutClosed = false
            let stderrClosed = false
            const state = { command, options, root, flushes: 0, killed: false }
            const stdout = new ReadableStream({ start(controller) { stdoutController = controller } })
            const stderr = new ReadableStream({ start(controller) { stderrController = controller } })
            const closeStreams = () => {
              if (!stdoutClosed) { stdoutClosed = true; stdoutController.close() }
              if (!stderrClosed) { stderrClosed = true; stderrController.close() }
            }
            const proc = {
              stdin: {
                write(value) { pending += value; return value.length },
                async flush() {
                  state.flushes += 1
                  const request = JSON.parse(pending.trim())
                  pending = ""
                  const behavior = nextBehavior(root)
                  if (behavior === "hang") {
                    return new Promise((_resolve, reject) => {
                      setTimeout(() => reject(new Error("test flush released")), 80)
                    })
                  }
                  if (behavior === "malformed-json") {
                    stdoutController.enqueue(encoder.encode("not-json\\n"))
                    return
                  }
                  if (behavior === "malformed-protocol") {
                    stdoutController.enqueue(encoder.encode('{"schema":2}\\n'))
                    return
                  }
                  if (behavior === "transport-close") {
                    stdoutClosed = true
                    stdoutController.close()
                    return
                  }
                  if (behavior === "incomplete") {
                    stdoutController.enqueue(encoder.encode('{"schema":2'))
                    stdoutClosed = true
                    stdoutController.close()
                    return
                  }
                  const bytes = encoder.encode(responseFor(request))
                  const split = bytes.findIndex((value) => value > 127) + 1
                  stdoutController.enqueue(bytes.slice(0, split))
                  await Promise.resolve()
                  stdoutController.enqueue(bytes.slice(split))
                },
                end() {},
              },
              stdout,
              stderr,
              kill() { state.killed = true; closeStreams() },
            }
            processes.push(state)
            return proc
          },
        }

        plan("/project", "malformed-json", "malformed-protocol", "success", "success", "success")
        const { DelphiCodebasePlugin } = await import("./.opencode/plugins/delphi_codebase.ts")
        const hooks = await DelphiCodebasePlugin({})
        const signal = new AbortController().signal
        const firstContext = { sessionID: "session-1", worktree: "/", directory: "/project", abort: signal }

        await assert.rejects(
          hooks.tool.delphi_codebase.execute({ action: "open" }, firstContext),
          /malformed JSON/,
        )
        await assert.rejects(
          hooks.tool.delphi_codebase.execute({ action: "open" }, firstContext),
          /malformed protocol response/,
        )
        const recovered = JSON.parse(await hooks.tool.delphi_codebase.execute({ action: "open" }, firstContext))
        assert.equal(recovered.result[0].name, "Grüße")
        assert.equal(processes.length, 1)
        assert.equal(processes[0].flushes, 3)
        assert.equal(processes[0].command.at(-1), "/project")
        assert.equal(processes[0].options.cwd, "/project")

        const compacted = { context: [] }
        await hooks["experimental.session.compacting"]({ sessionID: "session-1" }, compacted)
        assert.equal(compacted.context.length, 1)
        assert.match(compacted.context[0], /workspace_revision=revision-1/)
        assert.match(compacted.context[0], /src\\/Ünit.pas/)
        assert.equal(processes.length, 1)

        const secondContext = { sessionID: "session-1", worktree: "/other", directory: "/other", abort: signal }
        await hooks.tool.delphi_codebase.execute({ action: "open" }, secondContext)
        assert.equal(processes.length, 2)
        await hooks.event({ event: { type: "session.deleted", properties: { info: { id: "session-1" } } } })
        assert.deepEqual(processes.map((process) => process.killed), [true, true])

        plan("/repo/output/active-project", "success")
        const nestedContext = {
          sessionID: "nested-session",
          worktree: "/repo",
          directory: "/repo/output/active-project",
          abort: signal,
        }
        await hooks.tool.delphi_codebase.execute({ action: "open" }, nestedContext)
        const nestedProcess = processes.at(-1)
        assert.equal(nestedProcess.command.at(-1), "/repo/output/active-project")
        assert.equal(nestedProcess.options.cwd, "/repo/output/active-project")
        await hooks.event({ event: { type: "session.deleted", properties: { info: { id: "nested-session" } } } })

        const failures = []
        async function check(name, callback) {
          try {
            await callback()
          } catch (error) {
            failures.push(`${name}: ${error instanceof Error ? error.message : String(error)}`)
          }
        }

        await check("timeout bounds hanging flush", async () => {
          plan("/timeout", "hang", "success")
          const context = { sessionID: "timeout-session", worktree: "/timeout", directory: "/timeout", abort: signal }
          const started = Date.now()
          await assert.rejects(
            hooks.tool.delphi_codebase.execute({ action: "open" }, context),
            /request timed out/,
          )
          assert.ok(Date.now() - started < 70)
          assert.equal(processes.filter((process) => process.root === "/timeout").length, 1)
          assert.equal(processes.find((process) => process.root === "/timeout").killed, true)
          await hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          assert.equal(processes.filter((process) => process.root === "/timeout").length, 2)
        })

        await check("abort bounds hanging flush", async () => {
          plan("/abort", "hang", "success")
          const controller = new AbortController()
          const context = { sessionID: "abort-session", worktree: "/abort", directory: "/abort", abort: controller.signal }
          const pending = hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          setTimeout(() => controller.abort(), 5)
          await assert.rejects(pending, /request was cancelled/)
          assert.equal(processes.find((process) => process.root === "/abort").killed, true)
          const replacement = { ...context, abort: new AbortController().signal }
          await hooks.tool.delphi_codebase.execute({ action: "open" }, replacement)
          assert.equal(processes.filter((process) => process.root === "/abort").length, 2)
        })

        await check("transport closure replaces worker", async () => {
          plan("/transport", "transport-close", "success")
          const context = { sessionID: "transport-session", worktree: "/transport", directory: "/transport", abort: signal }
          await assert.rejects(
            hooks.tool.delphi_codebase.execute({ action: "open" }, context),
            /closed without a response/,
          )
          await hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          assert.equal(processes.filter((process) => process.root === "/transport").length, 2)
        })

        await check("incomplete response replaces worker", async () => {
          plan("/incomplete", "incomplete", "success")
          const context = { sessionID: "incomplete-session", worktree: "/incomplete", directory: "/incomplete", abort: signal }
          await assert.rejects(
            hooks.tool.delphi_codebase.execute({ action: "open" }, context),
            /incomplete response/,
          )
          await hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          assert.equal(processes.filter((process) => process.root === "/incomplete").length, 2)
        })

        await check("failed compaction evicts exact root", async () => {
          plan("/compaction-fail", "success", "transport-close", "success")
          plan("/compaction-stable", "success", "success", "success", "success")
          const failed = { sessionID: "compaction-session", worktree: "/compaction-fail", directory: "/compaction-fail", abort: signal }
          const stable = { sessionID: "compaction-session", worktree: "/compaction-stable", directory: "/compaction-stable", abort: signal }
          await hooks.tool.delphi_codebase.execute({ action: "open" }, failed)
          await hooks.tool.delphi_codebase.execute({ action: "open" }, stable)
          const compacted = { context: [] }
          await hooks["experimental.session.compacting"]({ sessionID: "compaction-session" }, compacted)
          assert.equal(compacted.context.length, 1)
          await hooks.tool.delphi_codebase.execute({ action: "open" }, failed)
          await hooks.tool.delphi_codebase.execute({ action: "open" }, stable)
          assert.equal(processes.filter((process) => process.root === "/compaction-fail").length, 2)
          assert.equal(processes.filter((process) => process.root === "/compaction-stable").length, 1)
        })

        await check("reusable compaction error keeps worker", async () => {
          plan("/compaction-reusable", "success", "malformed-json", "success")
          const context = { sessionID: "reusable-session", worktree: "/compaction-reusable", directory: "/compaction-reusable", abort: signal }
          await hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          await hooks["experimental.session.compacting"]({ sessionID: "reusable-session" }, { context: [] })
          await hooks.tool.delphi_codebase.execute({ action: "open" }, context)
          assert.equal(processes.filter((process) => process.root === "/compaction-reusable").length, 1)
        })

        await hooks.dispose()
        assert.ok(processes.every((process) => process.killed))
        if (unhandledRejections.length > 0) {
          failures.push(`unhandled rejections: ${unhandledRejections.map(String).join(", ")}`)
        }
        if (failures.length > 0) throw new Error(failures.join("\\n"))
        """,
    )

    completed = subprocess.run(
        [node, str(tmp_path / "runtime_harness.mjs")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_opencode_install_migrates_recognized_legacy_tool_and_is_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        'import { tool } from "@opencode-ai/plugin"\nexport default tool({ description: "Inspect a Delphi/Object Pascal codebase through python-delphi-lsp layered semantic views." })\n',
        encoding="utf-8",
    )

    from delphi_lsp.agent_templates import install_opencode_support

    first = install_opencode_support(tmp_path, python_executable=sys.executable, write_config=True)
    second = install_opencode_support(tmp_path, python_executable=sys.executable, write_config=True)

    assert not legacy.exists()
    assert first == second
    assert first[1] == tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts"
    config = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    agent = config["agent"]["vllm-delphi-codebase"]
    assert agent["tools"]["delphi_codebase"] is True
    assert agent["tools"]["skill"] is True
    assert agent["tools"]["lsp"] is False
    assert agent["permission"] == {
        "delphi_codebase": "allow",
        "lsp": "deny",
        "skill": {"*": "deny", "delphi-codebase-navigator": "allow"},
    }
    assert "load delphi-codebase-navigator first" in agent["prompt"]


def test_opencode_install_rejects_unrecognized_legacy_tool_without_force(tmp_path: Path) -> None:
    legacy = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("export default unexpectedTool\n", encoding="utf-8")

    from delphi_lsp.agent_templates import install_opencode_support

    with pytest.raises(FileExistsError, match="legacy"):
        install_opencode_support(tmp_path, python_executable=sys.executable)
    install_opencode_support(tmp_path, python_executable=sys.executable, force=True)
    assert not legacy.exists()


@pytest.mark.parametrize(
    "conflict_relative",
    [
        Path(".agents/skills/delphi-codebase-navigator/SKILL.md"),
        Path(".opencode/plugins/delphi_codebase.ts"),
    ],
)
def test_opencode_install_preflights_destinations_before_removing_legacy(
    tmp_path: Path,
    conflict_relative: Path,
) -> None:
    legacy = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    write_text(
        legacy,
        'import { tool } from "@opencode-ai/plugin"\n'
        'export default tool({ description: "Inspect through python-delphi-lsp layered semantic views." })',
    )
    conflict = tmp_path / conflict_relative
    write_text(conflict, "user-owned content")
    before = {legacy: legacy.read_bytes(), conflict: conflict.read_bytes()}

    from delphi_lsp.agent_templates import install_opencode_support

    with pytest.raises(FileExistsError):
        install_opencode_support(tmp_path, python_executable=sys.executable)

    assert legacy.read_bytes() == before[legacy]
    assert conflict.read_bytes() == before[conflict]
    other = (
        tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts"
        if "SKILL.md" in str(conflict_relative)
        else tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md"
    )
    assert not other.exists()


@pytest.mark.parametrize(
    "config_text, error_type",
    [
        ("{invalid json", json.JSONDecodeError),
        ('{"agent": []}', ValueError),
        ('{"agent": null}', ValueError),
    ],
)
def test_opencode_install_preflights_config_before_mutation(
    tmp_path: Path,
    config_text: str,
    error_type: type[Exception],
) -> None:
    legacy = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    write_text(
        legacy,
        'import { tool } from "@opencode-ai/plugin"\n'
        'export default tool({ description: "Inspect through python-delphi-lsp layered semantic views." })',
    )
    config = tmp_path / "opencode.json"
    config.write_text(config_text, encoding="utf-8")
    before = {legacy: legacy.read_bytes(), config: config.read_bytes()}

    from delphi_lsp.agent_templates import install_opencode_support

    with pytest.raises(error_type):
        install_opencode_support(
            tmp_path,
            python_executable=sys.executable,
            write_config=True,
        )

    assert legacy.read_bytes() == before[legacy]
    assert config.read_bytes() == before[config]
    assert not (tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md").exists()
    assert not (tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts").exists()


def test_opencode_install_rolls_back_if_config_write_fails(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    skill = tmp_path / ".agents" / "skills" / "delphi-codebase-navigator" / "SKILL.md"
    plugin = tmp_path / ".opencode" / "plugins" / "delphi_codebase.ts"
    config = tmp_path / "opencode.json"
    write_text(
        legacy,
        'import { tool } from "@opencode-ai/plugin"\n'
        'export default tool({ description: "Inspect through python-delphi-lsp layered semantic views." })',
    )
    write_text(skill, "existing skill")
    write_text(plugin, "existing plugin")
    config.write_text('{"agent": {}}\n', encoding="utf-8")
    paths = (legacy, skill, plugin, config)
    before = {path: path.read_bytes() for path in paths}
    original_write_text = Path.write_text

    def fail_after_partial_config_write(self, data, *args, **kwargs):  # noqa: ANN001
        if self == config:
            original_write_text(self, "partial", encoding="utf-8")
            raise PermissionError("simulated config write failure")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_after_partial_config_write)

    from delphi_lsp.agent_templates import install_opencode_support

    with pytest.raises(PermissionError, match="simulated config write failure"):
        install_opencode_support(
            tmp_path,
            python_executable=sys.executable,
            force=True,
            write_config=True,
        )

    assert {path: path.read_bytes() for path in paths} == before
