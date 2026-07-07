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


def test_builds_layered_markdown_without_exposing_routine_bodies(tmp_path: Path) -> None:
    make_project(tmp_path)

    index = build_codebase_index(tmp_path)
    markdown = render_layer(index, "unit", query="Worker", output_format="markdown")

    assert "Worker.pas" in markdown
    assert "TWorker" in markdown
    assert "Run" in markdown
    assert "body must not be exposed" not in markdown


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
            "symbols",
            "--query",
            "TWorker",
            "--format",
            "json",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["layer"] == "symbols"
    assert any(item["name"] == "TWorker" for item in payload["items"])


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
    assert "@opencode-ai/plugin" not in tool_text
    assert "grep" not in tool_text.casefold()
    assert str(skill) in completed.stdout
