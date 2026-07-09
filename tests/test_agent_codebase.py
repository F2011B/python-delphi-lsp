import json
import subprocess
import sys
import textwrap
from pathlib import Path

from delphiast.agent_layers import build_codebase_index, render_layer
from delphiast.project_indexer import ProjectIndexer


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
            "delphiast.agent_cli",
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


def test_opencode_install_writes_skill_and_custom_tool(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "delphiast.agent_cli",
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
    tool = tmp_path / ".opencode" / "tools" / "delphi_codebase.ts"
    assert skill.exists()
    assert tool.exists()
    assert "name: delphi-codebase-navigator" in skill.read_text(encoding="utf-8")
    tool_text = tool.read_text(encoding="utf-8")
    assert sys.executable in tool_text
    assert "delphiast.agent_cli" in tool_text
    assert '@opencode-ai/plugin' in tool_text
    assert "tool.schema.enum" in tool_text
    assert "tool.schema.string().optional()" in tool_text
    assert 'args.format ?? "markdown"' in tool_text
    assert '"implementation"' in tool_text
    assert 'context.directory !== "/"' in tool_text
    assert "process.cwd()" in tool_text
    assert "grep" not in tool_text.casefold()
    assert str(skill) in completed.stdout
