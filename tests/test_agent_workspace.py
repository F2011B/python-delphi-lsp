import json
import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import delphi_lsp.agent_workspace as agent_workspace_module
import delphi_lsp.project_discovery as project_discovery_module
from delphi_lsp.agent_protocol import AgentProtocolError, Focus, make_target_id
from delphi_lsp.agent_workspace import AgentUnit, AgentWorkspace
from delphi_lsp.parser import DelphiParser
from delphi_lsp.project_indexer import ProjectIndexer


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def test_open_auto_selects_one_project_with_deterministic_mapping(tmp_path: Path) -> None:
    write_text(tmp_path / "projects" / "Main.dpr", "program Main; begin end.")

    workspace = AgentWorkspace.open(tmp_path)

    expected_id = make_target_id("project", "projects/Main.dpr", "Main")
    assert len(workspace.projects) == 1
    project = workspace.projects[0]
    assert project.to_mapping() == {
        "project_id": expected_id,
        "name": "Main",
        "path": "projects/Main.dpr",
        "kind": "program",
    }
    assert workspace.active_project == project
    assert workspace.active_project_id == expected_id
    assert workspace.focus == Focus(project_id=expected_id, unit_id="", target_id="")


def test_selected_project_always_supplies_outline_transform_for_small_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "Main.dpr"
    source_path.write_text(
        "program Main; begin DoSmallWork; end.",
        encoding="utf-8",
    )
    captured_options: list[dict[str, object]] = []
    real_indexer = ProjectIndexer

    class RecordingIndexer:
        def __init__(self, **kwargs: object) -> None:
            captured_options.append(kwargs)
            self._delegate = real_indexer(**kwargs)

        def index(self, file_name: str):
            return self._delegate.index(file_name)

    monkeypatch.setattr(agent_workspace_module, "ProjectIndexer", RecordingIndexer)

    AgentWorkspace.open(tmp_path)

    assert len(captured_options) == 1
    source_transform = captured_options[0].get("source_transform")
    assert callable(source_transform)
    small_source = source_path.read_text(encoding="utf-8")
    assert source_transform(small_source) == "program Main; begin end."


def test_agent_outline_compacts_a_very_large_one_line_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "program Huge; begin " + ("DoWork; " * 50_000) + "end."
    assert len(source) > 400_000
    (tmp_path / "Huge.dpr").write_text(source, encoding="utf-8")
    transformed_sources: list[str] = []
    real_transform = agent_workspace_module._outline_agent_source

    def recording_transform(text: str) -> str:
        transformed = real_transform(text)
        transformed_sources.append(transformed)
        return transformed

    monkeypatch.setattr(agent_workspace_module, "_outline_agent_source", recording_transform)

    workspace = AgentWorkspace.open(tmp_path)

    assert [unit.name for unit in workspace.units] == ["Huge"]
    assert workspace.problems == ()
    assert transformed_sources == ["program Huge; begin end."]
    assert len(transformed_sources[0]) < 64


def test_agent_outline_transform_compacts_large_bodies_without_losing_project_syntax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text(
        tmp_path / "Bundle.dpk",
        """
        package Bundle;
        contains
          UnitA in 'UnitA.pas';
        end.
        """,
    )
    write_text(
        tmp_path / "Bundle.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Bundle.dpk</MainSource>
            <DCC_Define>KEEP_API</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    body_lines = "\n".join(
        f"          BodyOnlyCall{index};"
        for index in range(256)
    )
    write_text(
        tmp_path / "UnitA.pas",
        f"""
        unit UnitA;
        interface
        uses UnitB; // Keep the newline before the include directive.
        {{$IFDEF KEEP_API}}
        {{$I 'api.inc'}}
        {{$ENDIF}}
        const Greeting = 'begin end';
        {{ Preserve  begin case try end in this comment. }}
        (* Preserve  asm end in this comment. *)
        procedure Work;
        implementation
        procedure Work;
        begin
          case 1 of
            1:
              begin
                BodyOnlyCaseCall;
              end;
          end;
          BodyOnlyAfterCase;
          try
            BodyOnlyTryCall;
          except
            BodyOnlyExceptCall;
          end;
          BodyOnlyAfterExcept;
          try
            BodyOnlyFinallyTryCall;
          finally
            BodyOnlyFinallyCall;
          end;
          BodyOnlyAfterFinally;
{body_lines}
        end;
        end.
        """,
    )
    unit_b_source = (
        "unit UnitB; interface procedure DoThing; "
        "implementation procedure DoThing; begin end; end."
    )
    write_text(tmp_path / "UnitB.pas", unit_b_source)
    write_text(tmp_path / "api.inc", "const IncludedValue = 1;")
    transformed_sources: list[tuple[str, str]] = []
    real_transform = agent_workspace_module._outline_agent_source

    def recording_transform(text: str) -> str:
        transformed = real_transform(text)
        transformed_sources.append((text, transformed))
        return transformed

    monkeypatch.setattr(agent_workspace_module, "_outline_agent_source", recording_transform)

    workspace = AgentWorkspace.open(tmp_path)

    assert [unit.name for unit in workspace.units] == ["Bundle", "UnitA", "UnitB"]
    assert workspace.include_files == ({"name": "api.inc", "path": "api.inc"},)
    assert workspace.problems == ()
    assert len(transformed_sources) == 3

    transformed_unit_a = next(
        transformed
        for source, transformed in transformed_sources
        if "unit UnitA;" in source
    )
    assert "BodyOnlyCall" not in transformed_unit_a
    assert len(transformed_unit_a.splitlines()) < 20
    assert all(line and line == line.strip() for line in transformed_unit_a.splitlines())
    assert (
        "uses UnitB; // Keep the newline before the include directive.\n"
        "{$IFDEF KEEP_API}\n"
        "{$I 'api.inc'}\n"
        "{$ENDIF}"
    ) in transformed_unit_a
    assert "const Greeting = 'begin end';" in transformed_unit_a
    assert "{ Preserve  begin case try end in this comment. }" in transformed_unit_a
    assert "(* Preserve  asm end in this comment. *)" in transformed_unit_a

    transformed_unit_b = next(
        transformed
        for source, transformed in transformed_sources
        if "unit UnitB;" in source
    )
    assert transformed_unit_b == unit_b_source


@pytest.mark.parametrize("define_x", [False, True])
def test_agent_workspace_falls_back_safely_for_conditional_body_directives(
    tmp_path: Path,
    define_x: bool,
) -> None:
    main_source = textwrap.dedent(
        """
        program Main;
        uses UnitA in 'UnitA.pas';
        var Value: Integer;
        begin
          if Value = 0 then
          begin
        {$IFDEF X}
            UnitA.Run;
          end;
        {$ELSE}
            UnitA.Run;
          end;
        {$ENDIF}
          Value := 3;
        end.
        """
    ).strip() + "\n"
    (tmp_path / "Main.dpr").write_text(main_source, encoding="utf-8")
    write_text(
        tmp_path / "UnitA.pas",
        """
        unit UnitA;
        interface
        procedure Run;
        implementation
        procedure Run;
        begin
        end;
        end.
        """,
    )
    define_element = "<DCC_Define>X</DCC_Define>" if define_x else ""
    write_text(
        tmp_path / "Main.dproj",
        f"""
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            {define_element}
          </PropertyGroup>
        </Project>
        """,
    )

    workspace = AgentWorkspace.open(tmp_path)

    assert [unit.name for unit in workspace.units] == ["Main", "UnitA"]
    assert all(not unit.has_error for unit in workspace.units)
    assert not any(problem["kind"] == "cant_parse_file" for problem in workspace.problems)
    assert ("X" in workspace.defines) is define_x


def test_agent_outline_compactor_preserves_delphi_multiline_string_blocks() -> None:
    triple_block = (
        "'''\n"
        "don't  collapse   these\n"
        "&end begin case try class object\n"
        "{$IFDEF X}\n{$ENDIF}\n"
        "'''"
    )
    five_block = (
        "'''''\r\n"
        "don't  collapse   these\r\n"
        "// comment  text and ' apostrophe\r\n"
        "(*$IFDEF X*)\r\n(*$ENDIF*)\r\n"
        "'''''"
    )
    source = (
        "program AgentMultilineBlocks;\n"
        "const\n"
        f"  TripleValue = {triple_block};\n"
        f"  FiveValue = {five_block};\n"
        "begin\n"
        "  DoWork;\n"
        "end.\n"
    )

    transformed = agent_workspace_module._outline_agent_source(source)

    assert triple_block in transformed
    assert five_block in transformed
    assert "DoWork" not in transformed
    for defines in ((), ("X",)):
        DelphiParser(defines=defines).parse(transformed, "agent_multiline_blocks.dpr")


def test_workspace_exposes_immutable_effective_context_accessors(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>AGENT_CONTEXT</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )

    workspace = AgentWorkspace.open(tmp_path)

    assert workspace.root == tmp_path.resolve()
    assert workspace.search_paths == (str((tmp_path / "src").resolve()),)
    assert workspace.include_paths == (str((tmp_path / "include").resolve()),)
    assert workspace.defines == ("AGENT_CONTEXT",)
    assert isinstance(workspace.search_paths, tuple)
    assert isinstance(workspace.include_paths, tuple)
    assert isinstance(workspace.defines, tuple)
    with pytest.raises(AttributeError):
        workspace.root = Path("changed")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        workspace.search_paths = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        workspace.include_paths = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        workspace.defines = ()  # type: ignore[misc]


def test_open_selects_an_explicit_project_from_a_multi_project_workspace(tmp_path: Path) -> None:
    write_text(tmp_path / "A.dpr", "program A; begin end.")
    selected_path = tmp_path / "nested" / "B.dpr"
    write_text(selected_path, "program B; begin end.")

    workspace = AgentWorkspace.open(tmp_path, project_file="nested/B.dpr")

    expected_id = make_target_id("project", "nested/B.dpr", "B")
    assert [project.name for project in workspace.projects] == ["B"]
    assert workspace.active_project_id == expected_id
    assert [unit.name for unit in workspace.units] == ["B"]


def test_selected_project_exposes_only_reachable_units_with_deterministic_ids(tmp_path: Path) -> None:
    write_text(
        tmp_path / "projects" / "Main.dpr",
        """
        program Main;

        uses
          UnitA in '..\\src\\UnitA.pas';

        begin
        end.
        """,
    )
    write_text(
        tmp_path / "src" / "UnitA.pas",
        """
        unit UnitA;

        interface

        uses UnitB;

        implementation

        end.
        """,
    )
    write_text(tmp_path / "src" / "UnitB.pas", "unit UnitB; interface implementation end.")
    write_text(tmp_path / "Noise.pas", "unit Noise; interface implementation end.")

    workspace = AgentWorkspace.open(tmp_path)

    assert all(isinstance(unit, AgentUnit) for unit in workspace.units)
    assert [unit.to_mapping() for unit in workspace.units] == [
        {
            "unit_id": make_target_id("unit", "projects/Main.dpr", "Main"),
            "name": "Main",
            "path": "projects/Main.dpr",
            "has_error": False,
        },
        {
            "unit_id": make_target_id("unit", "src/UnitA.pas", "UnitA"),
            "name": "UnitA",
            "path": "src/UnitA.pas",
            "has_error": False,
        },
        {
            "unit_id": make_target_id("unit", "src/UnitB.pas", "UnitB"),
            "name": "UnitB",
            "path": "src/UnitB.pas",
            "has_error": False,
        },
    ]
    with pytest.raises(FrozenInstanceError):
        workspace.active_project.path = "changed.dpr"  # type: ignore[misc,union-attr]
    with pytest.raises(FrozenInstanceError):
        workspace.units[0].path = "changed.pas"  # type: ignore[misc]


def test_multiple_projects_stay_inactive_until_selection_and_remain_isolated(tmp_path: Path) -> None:
    write_text(
        tmp_path / "A.dpr",
        """
        program A;
        uses AOnly in 'a/AOnly.pas';
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "A.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>A.dpr</MainSource>
            <DCC_IncludePath>a/includes</DCC_IncludePath>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(
        tmp_path / "a" / "AOnly.pas",
        """
        unit AOnly;
        interface
        {$I 'A.inc'}
        implementation
        end.
        """,
    )
    write_text(tmp_path / "a" / "includes" / "A.inc", "const AValue = 1;")
    write_text(
        tmp_path / "B.dpr",
        """
        program B;
        uses BOnly in 'b/BOnly.pas';
        begin
        end.
        """,
    )
    write_text(tmp_path / "b" / "BOnly.pas", "unit BOnly; interface implementation end.")
    write_text(tmp_path / "Noise.pas", "unit Noise; interface implementation end.")

    workspace = AgentWorkspace.open(tmp_path)
    project_ids = {project.name: project.project_id for project in workspace.projects}

    assert workspace.active_project is None
    assert workspace.active_project_id == ""
    assert workspace.focus == Focus()
    assert workspace.units == ()

    workspace.select_project(project_ids["A"])

    assert [unit.name for unit in workspace.units] == ["A", "AOnly"]
    assert workspace.include_files == (
        {"name": "A.inc", "path": "a/includes/A.inc"},
    )
    assert workspace.focus == Focus(project_id=project_ids["A"], unit_id="", target_id="")

    workspace.select_project(project_ids["B"])

    assert [unit.name for unit in workspace.units] == ["B", "BOnly"]
    assert workspace.include_files == ()
    assert workspace.focus == Focus(project_id=project_ids["B"], unit_id="", target_id="")


def test_include_files_returns_defensive_mapping_copies(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'UnitA.pas';
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "UnitA.pas",
        """
        unit UnitA;
        interface
        {$I 'shared.inc'}
        implementation
        end.
        """,
    )
    write_text(tmp_path / "shared.inc", "const SharedValue = 1;")
    workspace = AgentWorkspace.open(tmp_path)

    returned = workspace.include_files
    returned[0]["path"] = "mutated.inc"

    assert workspace.include_files == (
        {"name": "shared.inc", "path": "shared.inc"},
    )


def test_select_project_rejects_an_unknown_project_id(tmp_path: Path) -> None:
    write_text(tmp_path / "A.dpr", "program A; begin end.")
    write_text(tmp_path / "B.dpr", "program B; begin end.")
    workspace = AgentWorkspace.open(tmp_path)

    with pytest.raises(AgentProtocolError) as caught:
        workspace.select_project("missing-project")

    assert caught.value.code == "project_not_found"
    assert caught.value.message == "Project not found: missing-project."
    assert workspace.active_project_id == ""
    assert workspace.focus == Focus()


def test_reselect_reuses_project_index_until_a_reachable_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'UnitA.pas';
        begin
        end.
        """,
    )
    unit_path = tmp_path / "UnitA.pas"
    write_text(unit_path, "unit UnitA; interface implementation end.")

    calls = 0
    original_index = ProjectIndexer.index

    def counting_index(self: ProjectIndexer, file_name: str):
        nonlocal calls
        calls += 1
        return original_index(self, file_name)

    monkeypatch.setattr(ProjectIndexer, "index", counting_index)

    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    workspace.select_project(project_id)

    assert calls == 1

    unit_path.write_text(
        unit_path.read_text(encoding="utf-8") + "// changed\n",
        encoding="utf-8",
    )
    workspace.select_project(project_id)

    assert calls == 2


def test_reselect_indexes_a_newly_satisfiable_dependency(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses MissingUnit;
        begin
        end.
        """,
    )
    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id

    assert [unit.name for unit in workspace.units] == ["Main"]
    assert any(problem["path"] == "MissingUnit" for problem in workspace.problems)

    write_text(
        tmp_path / "MissingUnit.pas",
        "unit MissingUnit; interface implementation end.",
    )
    workspace.select_project(project_id)

    assert [unit.name for unit in workspace.units] == ["Main", "MissingUnit"]
    assert not any(problem.get("path") == "MissingUnit" for problem in workspace.problems)


def test_environment_search_path_change_replaces_cached_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_path = tmp_path / "old_lib"
    new_path = tmp_path / "new_lib"
    write_text(old_path / "EnvUnit.pas", "unit EnvUnit; interface implementation end.")
    write_text(new_path / "EnvUnit.pas", "unit EnvUnit; interface implementation end.")
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses EnvUnit;
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_UnitSearchPath>$AGENT_WORKSPACE_LIB</DCC_UnitSearchPath>
          </PropertyGroup>
        </Project>
        """,
    )
    monkeypatch.setenv("AGENT_WORKSPACE_LIB", str(old_path))
    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    original_revision = workspace.workspace_revision

    assert {unit.path for unit in workspace.units} == {"Main.dpr", "old_lib/EnvUnit.pas"}

    monkeypatch.setenv("AGENT_WORKSPACE_LIB", str(new_path))
    workspace.select_project(project_id)

    assert {unit.path for unit in workspace.units} == {"Main.dpr", "new_lib/EnvUnit.pas"}
    assert workspace.workspace_revision != original_revision


def test_workspace_revision_tracks_a_newly_satisfiable_dependency(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses MissingUnit;
        begin
        end.
        """,
    )
    workspace = AgentWorkspace.open(tmp_path)
    original_revision = workspace.workspace_revision

    write_text(
        tmp_path / "MissingUnit.pas",
        "unit MissingUnit; interface implementation end.",
    )

    assert workspace.workspace_revision != original_revision


def test_nested_include_creation_invalidates_revision_and_project_cache(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_IncludePath>include</DCC_IncludePath>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(
        tmp_path / "src" / "UnitA.pas",
        """
        unit UnitA;
        interface
        {$I 'nested/new.inc'}
        implementation
        end.
        """,
    )
    (tmp_path / "include").mkdir()
    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    original_revision = workspace.workspace_revision

    assert workspace.include_files == ()

    write_text(tmp_path / "include" / "nested" / "new.inc", "const NewValue = 1;")

    assert workspace.workspace_revision != original_revision
    workspace.select_project(project_id)
    assert workspace.include_files == (
        {"name": "nested/new.inc", "path": "include/nested/new.inc"},
    )


def test_source_relative_nested_include_invalidates_revision_and_project_cache(
    tmp_path: Path,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "src" / "UnitA.pas",
        """
        unit UnitA;
        interface
        {$I 'nested/new.inc'}
        implementation
        end.
        """,
    )
    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    original_revision = workspace.workspace_revision

    assert workspace.include_files == ()

    write_text(tmp_path / "src" / "nested" / "new.inc", "const NewValue = 1;")

    assert workspace.workspace_revision != original_revision
    workspace.select_project(project_id)
    assert workspace.include_files == (
        {"name": "nested/new.inc", "path": "src/nested/new.inc"},
    )


def test_explicit_nested_unit_path_invalidates_revision_without_recursive_pascal_scan(
    tmp_path: Path,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "src" / "UnitA.pas",
        """
        unit UnitA;
        interface
        uses UnitB in 'nested/UnitB.pas';
        implementation
        end.
        """,
    )
    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    original_revision = workspace.workspace_revision

    assert [unit.name for unit in workspace.units] == ["Main", "UnitA"]
    assert any(problem.get("path") == "UnitB" for problem in workspace.problems)

    write_text(
        tmp_path / "src" / "nested" / "Noise.pas",
        "unit Noise; interface implementation end.",
    )
    assert workspace.workspace_revision == original_revision

    write_text(
        tmp_path / "src" / "nested" / "UnitB.pas",
        "unit UnitB; interface implementation end.",
    )

    assert workspace.workspace_revision != original_revision
    workspace.select_project(project_id)
    assert [unit.name for unit in workspace.units] == ["Main", "UnitA", "UnitB"]
    assert not any(problem.get("path") == "UnitB" for problem in workspace.problems)


@pytest.mark.parametrize("config_suffix", [".dproj", ".cfg", ".dof"])
def test_new_and_changed_project_config_invalidates_revision_and_is_applied(
    tmp_path: Path,
    config_suffix: str,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses ConfigUnit;
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "old_lib" / "ConfigUnit.pas",
        "unit ConfigUnit; interface implementation end.",
    )
    write_text(
        tmp_path / "new_lib" / "ConfigUnit.pas",
        "unit ConfigUnit; interface implementation end.",
    )
    config_path = tmp_path / f"Main{config_suffix}"

    def config_text(search_path: str, define: str) -> str:
        if config_suffix == ".dproj":
            return f"""
            <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
              <PropertyGroup>
                <MainSource>Main.dpr</MainSource>
                <DCC_UnitSearchPath>{search_path}</DCC_UnitSearchPath>
                <DCC_Define>{define}</DCC_Define>
              </PropertyGroup>
            </Project>
            """
        return f"-U{search_path}\n-D{define}"

    workspace = AgentWorkspace.open(tmp_path)
    project_id = workspace.active_project_id
    initial_revision = workspace.workspace_revision

    write_text(config_path, config_text("old_lib", "CONFIG_ADDED"))

    assert workspace.workspace_revision != initial_revision
    workspace.select_project(project_id)
    assert {unit.path for unit in workspace.units} == {"Main.dpr", "old_lib/ConfigUnit.pas"}
    assert [entry["define"] for entry in workspace.define_entries] == ["CONFIG_ADDED"]

    added_revision = workspace.workspace_revision
    write_text(config_path, config_text("new_lib", "CONFIG_CHANGED_LONG"))

    assert workspace.workspace_revision != added_revision
    workspace.select_project(project_id)
    assert {unit.path for unit in workspace.units} == {"Main.dpr", "new_lib/ConfigUnit.pas"}
    assert [entry["define"] for entry in workspace.define_entries] == ["CONFIG_CHANGED_LONG"]


def test_active_project_revision_uses_explicit_discovery_without_recursive_pascal_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text(tmp_path / "Main.dpr", "program Main; begin end.")
    workspace = AgentWorkspace.open(tmp_path)

    discovery_calls: list[dict[str, object]] = []
    root_traversals = 0
    original_discover = agent_workspace_module.discover_delphi_project
    original_rglob = Path.rglob

    def recording_discover(*args, **kwargs):
        discovery_calls.append(dict(kwargs))
        return original_discover(*args, **kwargs)

    def counting_rglob(path: Path, pattern: str):
        nonlocal root_traversals
        if path.resolve() == tmp_path.resolve():
            root_traversals += 1
        return original_rglob(path, pattern)

    monkeypatch.setattr(agent_workspace_module, "discover_delphi_project", recording_discover)
    monkeypatch.setattr(Path, "rglob", counting_rglob)

    original_revision = workspace.workspace_revision
    write_text(
        tmp_path / "nested" / "Noise.pas",
        "unit Noise; interface implementation end.",
    )

    assert workspace.workspace_revision == original_revision
    assert discovery_calls == [
        {
            "project_file": (tmp_path / "Main.dpr").resolve(),
            "scan_workspace_sources": False,
        },
        {
            "project_file": (tmp_path / "Main.dpr").resolve(),
            "scan_workspace_sources": False,
        },
    ]
    assert root_traversals == 2


def test_ids_and_workspace_revision_are_deterministic_and_revision_tracks_source_changes(
    tmp_path: Path,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    unit_path = tmp_path / "src" / "UnitA.pas"
    write_text(unit_path, "unit UnitA; interface implementation end.")

    first = AgentWorkspace.open(tmp_path)
    second = AgentWorkspace.open(tmp_path)

    assert [project.project_id for project in first.projects] == [
        project.project_id for project in second.projects
    ]
    assert [unit.unit_id for unit in first.units] == [unit.unit_id for unit in second.units]
    assert first.workspace_revision == second.workspace_revision
    assert first.workspace_revision.startswith("workspace_v2_")

    original_revision = first.workspace_revision
    unit_path.write_text(
        unit_path.read_text(encoding="utf-8") + "// revision change\n",
        encoding="utf-8",
    )

    assert first.workspace_revision != original_revision


def test_workspace_exposes_project_path_and_define_provenance_as_json(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'src/UnitA.pas';
        begin
        end.
        """,
    )
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_UnitSearchPath>lib</DCC_UnitSearchPath>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>DPROJ_ONLY</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(tmp_path / "Main.cfg", "-Ucfg_lib\n-Icfg_include\n-DCFG_ONLY")
    write_text(tmp_path / "Main.dof", "-Udof_lib\n-Idof_include\n-DDOF_ONLY")

    workspace = AgentWorkspace.open(tmp_path)

    assert workspace.search_path_entries == (
        {"path": "src", "origins": ["Main.dpr"]},
        {"path": "lib", "origins": ["Main.dproj"]},
        {"path": "cfg_lib", "origins": ["Main.cfg"]},
        {"path": "dof_lib", "origins": ["Main.dof"]},
    )
    assert workspace.include_path_entries == (
        {"path": "include", "origins": ["Main.dproj"]},
        {"path": "cfg_include", "origins": ["Main.cfg"]},
        {"path": "dof_include", "origins": ["Main.dof"]},
    )
    assert workspace.define_entries == (
        {"define": "DPROJ_ONLY", "origins": ["Main.dproj"]},
        {"define": "CFG_ONLY", "origins": ["Main.cfg"]},
        {"define": "DOF_ONLY", "origins": ["Main.dof"]},
    )
    json.dumps(
        {
            "search": workspace.search_path_entries,
            "include": workspace.include_path_entries,
            "defines": workspace.define_entries,
        },
        allow_nan=False,
    )


def test_workspace_combines_discovery_and_selected_project_problems(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses MissingUnit;
        begin
        end.
        """,
    )
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_UnitSearchPath>$(UNKNOWN_ROOT)/lib</DCC_UnitSearchPath>
          </PropertyGroup>
        </Project>
        """,
    )

    workspace = AgentWorkspace.open(tmp_path)

    assert len(workspace.problems) == 2
    discovery_problem, project_problem = workspace.problems
    assert discovery_problem["kind"] == "unresolved_macro"
    assert "$(UNKNOWN_ROOT)" in discovery_problem["message"]
    assert discovery_problem["origin"] == "Main.dproj"
    assert project_problem == {
        "kind": "cant_find_file",
        "message": "Unit not found: MissingUnit",
        "origin": "Main.dpr",
        "path": "MissingUnit",
    }
    json.dumps(workspace.problems, allow_nan=False)


def test_workspace_deduplicates_identical_active_project_indexer_problems(
    tmp_path: Path,
) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses
          UnitA in 'UnitA.pas',
          UnitB in 'UnitB.pas';
        begin
        end.
        """,
    )
    for unit_name in ("UnitA", "UnitB"):
        write_text(
            tmp_path / f"{unit_name}.pas",
            f"""
            unit {unit_name};
            interface
            uses MissingUnit;
            implementation
            end.
            """,
        )

    workspace = AgentWorkspace.open(tmp_path)

    missing_problems = [
        problem
        for problem in workspace.problems
        if problem.get("path") == "MissingUnit"
    ]
    assert missing_problems == [
        {
            "kind": "cant_find_file",
            "message": "Unit not found: MissingUnit",
            "origin": "Main.dpr",
            "path": "MissingUnit",
        }
    ]


def test_active_project_problems_exclude_unselected_project_discovery_problems(
    tmp_path: Path,
) -> None:
    write_text(tmp_path / "A.dpr", "program A; begin end.")
    write_text(tmp_path / "B.dpr", "program B; begin end.")
    write_text(
        tmp_path / "B.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>B.dpr</MainSource>
            <DCC_UnitSearchPath>$(B_ONLY_ROOT)/lib</DCC_UnitSearchPath>
          </PropertyGroup>
        </Project>
        """,
    )

    workspace = AgentWorkspace.open(tmp_path)
    project_ids = {project.name: project.project_id for project in workspace.projects}

    assert len(workspace.problems) == 1
    assert workspace.problems[0]["origin"] == "B.dproj"
    assert "$(B_ONLY_ROOT)" in workspace.problems[0]["message"]

    workspace.select_project(project_ids["A"])

    assert workspace.problems == ()


def test_open_uses_a_workspace_fallback_for_standalone_units(tmp_path: Path) -> None:
    write_text(
        tmp_path / "src" / "Alpha.pas",
        """
        unit Alpha;
        interface
        uses Beta;
        {$I 'common.inc'}
        implementation
        end.
        """,
    )
    write_text(tmp_path / "src" / "Beta.pas", "unit Beta; interface implementation end.")
    write_text(tmp_path / "loose" / "Loose.pas", "unit Loose; interface implementation end.")
    write_text(tmp_path / "includes" / "common.inc", "const CommonValue = 1;")
    write_text(
        tmp_path / "Standalone.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <DCC_UnitSearchPath>src</DCC_UnitSearchPath>
            <DCC_IncludePath>includes</DCC_IncludePath>
          </PropertyGroup>
        </Project>
        """,
    )

    workspace = AgentWorkspace.open(tmp_path)

    fallback_id = make_target_id("project", "", "workspace")
    assert [project.to_mapping() for project in workspace.projects] == [
        {
            "project_id": fallback_id,
            "name": "Workspace",
            "path": ".",
            "kind": "workspace",
        }
    ]
    assert workspace.active_project_id == fallback_id
    assert workspace.focus == Focus(project_id=fallback_id, unit_id="", target_id="")
    assert [unit.name for unit in workspace.units] == ["Alpha", "Beta", "Loose"]
    assert workspace.include_files == (
        {"name": "common.inc", "path": "includes/common.inc"},
    )
    assert {entry["path"] for entry in workspace.search_path_entries} == {"src", "loose"}
    assert workspace.include_path_entries == (
        {"path": "includes", "origins": ["workspace include scan"]},
    )


def test_workspace_fallback_discovers_once_and_builds_a_flat_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text(tmp_path / "src" / "Alpha.pas", "unit Alpha; interface implementation end.")
    write_text(tmp_path / "other" / "Beta.pas", "unit Beta; interface implementation end.")
    write_text(tmp_path / "include" / "used.inc", "const UsedValue = 1;")
    write_text(tmp_path / "include" / "orphan.inc", "const OrphanValue = 2;")

    discovery_calls = 0
    source_scan_calls = 0
    index_calls = 0
    original_discover = agent_workspace_module.discover_delphi_project
    original_scan_sources = project_discovery_module._scan_sources
    original_index = ProjectIndexer.index

    def counting_discover(*args, **kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        return original_discover(*args, **kwargs)

    def counting_scan_sources(*args, **kwargs):
        nonlocal source_scan_calls
        source_scan_calls += 1
        return original_scan_sources(*args, **kwargs)

    def counting_index(self: ProjectIndexer, file_name: str):
        nonlocal index_calls
        index_calls += 1
        return original_index(self, file_name)

    monkeypatch.setattr(agent_workspace_module, "discover_delphi_project", counting_discover)
    monkeypatch.setattr(project_discovery_module, "_scan_sources", counting_scan_sources)
    monkeypatch.setattr(ProjectIndexer, "index", counting_index)

    workspace = AgentWorkspace.open(tmp_path)

    assert discovery_calls == 1
    assert source_scan_calls == 1
    assert index_calls == 0
    assert [unit.name for unit in workspace.units] == ["Alpha", "Beta"]
    assert workspace.include_files == (
        {"name": "orphan.inc", "path": "include/orphan.inc"},
        {"name": "used.inc", "path": "include/used.inc"},
    )


def test_workspace_fallback_refreshes_added_nested_and_deleted_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha_path = tmp_path / "src" / "Alpha.pas"
    write_text(alpha_path, "unit Alpha; interface implementation end.")

    discovery_calls = 0
    index_calls = 0
    original_discover = agent_workspace_module.discover_delphi_project
    original_index = ProjectIndexer.index

    def counting_discover(*args, **kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        return original_discover(*args, **kwargs)

    def counting_index(self: ProjectIndexer, file_name: str):
        nonlocal index_calls
        index_calls += 1
        return original_index(self, file_name)

    monkeypatch.setattr(agent_workspace_module, "discover_delphi_project", counting_discover)
    monkeypatch.setattr(ProjectIndexer, "index", counting_index)

    workspace = AgentWorkspace.open(tmp_path)
    workspace_id = workspace.active_project_id
    initial_revision = workspace.workspace_revision

    write_text(tmp_path / "src" / "Beta.pas", "unit Beta; interface implementation end.")

    assert workspace.workspace_revision != initial_revision
    workspace.select_project(workspace_id)
    assert [unit.name for unit in workspace.units] == ["Alpha", "Beta"]

    beta_revision = workspace.workspace_revision
    write_text(
        tmp_path / "new" / "nested" / "Gamma.pas",
        "unit Gamma; interface implementation end.",
    )

    assert workspace.workspace_revision != beta_revision
    workspace.select_project(workspace_id)
    assert [unit.name for unit in workspace.units] == ["Alpha", "Beta", "Gamma"]

    gamma_revision = workspace.workspace_revision
    alpha_path.unlink()

    assert workspace.workspace_revision != gamma_revision
    workspace.select_project(workspace_id)
    assert [unit.name for unit in workspace.units] == ["Beta", "Gamma"]
    assert discovery_calls == 1
    assert index_calls == 0


def test_fallback_revision_and_reselection_use_fresh_source_only_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text(tmp_path / "src" / "Alpha.pas", "unit Alpha; interface implementation end.")
    workspace = AgentWorkspace.open(tmp_path)
    workspace_id = workspace.active_project_id

    source_discovery_calls = 0
    index_calls = 0
    original_source_discovery = agent_workspace_module.discover_workspace_sources
    original_index = ProjectIndexer.index

    def counting_source_discovery(*args, **kwargs):
        nonlocal source_discovery_calls
        source_discovery_calls += 1
        return original_source_discovery(*args, **kwargs)

    def counting_index(self: ProjectIndexer, file_name: str):
        nonlocal index_calls
        index_calls += 1
        return original_index(self, file_name)

    monkeypatch.setattr(
        agent_workspace_module,
        "discover_workspace_sources",
        counting_source_discovery,
    )
    monkeypatch.setattr(ProjectIndexer, "index", counting_index)

    _ = workspace.workspace_revision
    workspace.select_project(workspace_id)

    assert source_discovery_calls == 2
    assert index_calls == 0
