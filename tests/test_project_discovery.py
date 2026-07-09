import textwrap
import os
from pathlib import Path

from delphiast.project_discovery import discover_delphi_project
from delphiast.project_indexer import ProjectIndexer
from delphiast.lsp_server import LspWorkspaceState, WorkspaceConfig


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def test_discovery_can_skip_workspace_source_scan(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;

        uses
          UnitA in 'src/UnitA.pas';

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
            <DCC_UnitSearchPath>lib</DCC_UnitSearchPath>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")
    write_text(tmp_path / "Noise.pas", "unit Noise; interface implementation end.")

    discovery = discover_delphi_project(tmp_path, scan_workspace_sources=False)

    assert discovery.project_files == [str((tmp_path / "Main.dpr").resolve())]
    assert discovery.config_files == [str((tmp_path / "Main.dproj").resolve())]
    assert str((tmp_path / "src").resolve()) in discovery.search_paths
    assert str((tmp_path / "lib").resolve()) in discovery.search_paths
    assert discovery.source_files == []
    assert discovery.unit_paths == {}


def test_discovery_records_deterministic_path_and_define_origins(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;

        uses
          UnitA in 'src/UnitA.pas';

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
            <DCC_UnitSearchPath>src;lib</DCC_UnitSearchPath>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>SHARED;DPROJ_ONLY</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(
        tmp_path / "Main.cfg",
        """
        -Usrc;cfg_lib
        -Iinclude
        -DSHARED;CFG_ONLY
        """,
    )
    write_text(
        tmp_path / "Main.dof",
        """
        -Ulib;dof_lib
        -Iinclude
        -DSHARED;DOF_ONLY
        """,
    )
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")

    discovery = discover_delphi_project(
        tmp_path,
        project_file=tmp_path / "Main.dpr",
        search_paths=["src"],
        include_paths=["include"],
        defines=["SHARED", "MANUAL_ONLY"],
        scan_workspace_sources=False,
    )

    main_dpr = str((tmp_path / "Main.dpr").resolve())
    main_dproj = str((tmp_path / "Main.dproj").resolve())
    main_cfg = str((tmp_path / "Main.cfg").resolve())
    main_dof = str((tmp_path / "Main.dof").resolve())
    src = str((tmp_path / "src").resolve())
    lib = str((tmp_path / "lib").resolve())
    include = str((tmp_path / "include").resolve())

    assert list(discovery.search_path_origins) == discovery.search_paths
    assert list(discovery.include_path_origins) == discovery.include_paths
    assert list(discovery.define_origins) == discovery.defines
    assert discovery.search_path_origins[src] == [
        "manual search path",
        main_dpr,
        main_dproj,
        main_cfg,
    ]
    assert discovery.search_path_origins[lib] == [main_dproj, main_dof]
    assert discovery.include_path_origins[include] == [
        "manual include path",
        main_dproj,
        main_cfg,
        main_dof,
    ]
    assert discovery.define_origins["SHARED"] == [
        "manual define",
        main_dproj,
        main_cfg,
        main_dof,
    ]
    assert discovery.define_origins["MANUAL_ONLY"] == ["manual define"]
    assert discovery.define_origins["DPROJ_ONLY"] == [main_dproj]
    assert discovery.define_origins["CFG_ONLY"] == [main_cfg]
    assert discovery.define_origins["DOF_ONLY"] == [main_dof]


def test_scan_free_discovery_resolves_project_config_paths_and_supported_macros(
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
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_UnitSearchPath>$(PROJECTDIR)/lib;$(MSBuildProjectDirectory)/shared</DCC_UnitSearchPath>
            <DCC_IncludePath>$(PROJECT_DIR)/include;$(MSBuildThisFileDirectory)/generated</DCC_IncludePath>
            <DCC_Define>DPROJ_DEFINE</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(tmp_path / "Main.cfg", "-Ucfg_lib\n-Icfg_include\n-DCFG_DEFINE")
    write_text(tmp_path / "Main.dof", "-Udof_lib\n-Idof_include\n-DDOF_DEFINE")
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")

    discovery = discover_delphi_project(tmp_path, scan_workspace_sources=False)

    assert {Path(path) for path in discovery.search_paths} == {
        (tmp_path / "src").resolve(),
        (tmp_path / "lib").resolve(),
        (tmp_path / "shared").resolve(),
        (tmp_path / "cfg_lib").resolve(),
        (tmp_path / "dof_lib").resolve(),
    }
    assert {Path(path) for path in discovery.include_paths} == {
        (tmp_path / "include").resolve(),
        (tmp_path / "generated").resolve(),
        (tmp_path / "cfg_include").resolve(),
        (tmp_path / "dof_include").resolve(),
    }
    assert discovery.defines == ["DPROJ_DEFINE", "CFG_DEFINE", "DOF_DEFINE"]
    assert discovery.source_files == []
    assert discovery.unit_paths == {}
    assert not discovery.problems


def test_default_source_scan_records_workspace_origins(tmp_path: Path) -> None:
    write_text(tmp_path / "Main.dpr", "program Main; begin end.")
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")
    write_text(tmp_path / "include" / "common.inc", "const CommonValue = 1;")

    discovery = discover_delphi_project(tmp_path)

    root = str(tmp_path.resolve())
    src = str((tmp_path / "src").resolve())
    include = str((tmp_path / "include").resolve())
    assert discovery.search_path_origins[root] == ["workspace source scan"]
    assert discovery.search_path_origins[src] == ["workspace source scan"]
    assert discovery.include_path_origins[include] == ["workspace include scan"]


def test_discovers_paths_defines_and_units_without_manual_environment(tmp_path: Path) -> None:
    write_text(
        tmp_path / "Main.dpr",
        """
        program Main;

        uses
          UnitA in 'src/UnitA.pas',
          UnitB;

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
            <DCC_UnitSearchPath>src;lib</DCC_UnitSearchPath>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>MSWINDOWS;USE_FAST</DCC_Define>
          </PropertyGroup>
          <ItemGroup>
            <DCCReference Include="src/UnitA.pas" />
          </ItemGroup>
        </Project>
        """,
    )
    write_text(
        tmp_path / "Main.cfg",
        """
        -Ucfgsrc;cfg_lib
        -Icfg_include
        -DDEBUG;TRACE
        """,
    )
    write_text(
        tmp_path / "src" / "UnitA.pas",
        """
        unit UnitA;

        interface
        {$I 'build.inc'}

        implementation

        end.
        """,
    )
    write_text(
        tmp_path / "src" / "UnitB.pas",
        """
        unit UnitB;

        interface

        implementation

        end.
        """,
    )
    write_text(tmp_path / "include" / "build.inc", "const IncludedValue = 1;")

    discovery = discover_delphi_project(tmp_path, project_file=tmp_path / "Main.dpr")

    search_paths = {Path(path) for path in discovery.search_paths}
    include_paths = {Path(path) for path in discovery.include_paths}
    assert tmp_path / "src" in search_paths
    assert tmp_path / "lib" in search_paths
    assert tmp_path / "cfgsrc" in search_paths
    assert tmp_path / "cfg_lib" in search_paths
    assert tmp_path / "include" in include_paths
    assert tmp_path / "cfg_include" in include_paths
    assert {"MSWINDOWS", "USE_FAST", "DEBUG", "TRACE"}.issubset(set(discovery.defines))
    assert discovery.unit_paths["unitb"] == [str((tmp_path / "src" / "UnitB.pas").resolve())]

    indexer = ProjectIndexer(
        search_paths=discovery.search_paths,
        include_paths=discovery.include_paths,
        defines=discovery.defines,
    )
    result = indexer.index(str(tmp_path / "Main.dpr"))

    assert {"Main", "UnitA", "UnitB"}.issubset({unit.name for unit in result.parsed_units})
    assert "build.inc" in {include.name for include in result.include_files}
    assert not result.not_found_units


def test_discovery_reports_unresolved_external_project_macros(tmp_path: Path) -> None:
    write_text(tmp_path / "Main.dpr", "program Main; begin end.")
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <DCC_UnitSearchPath>$(BDS)\\lib;src</DCC_UnitSearchPath>
            <DCC_Define>$(DCC_Define);MSWINDOWS</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    (tmp_path / "src").mkdir()

    discovery = discover_delphi_project(tmp_path, project_file=tmp_path / "Main.dpr")

    assert str((tmp_path / "src").resolve()) in discovery.search_paths
    assert "MSWINDOWS" in discovery.defines
    assert "$(DCC_Define)" not in discovery.defines
    assert any(problem.kind == "unresolved_macro" and "$(BDS)" in problem.message for problem in discovery.problems)
    assert any(problem.kind == "unresolved_macro" and "$(DCC_Define)" in problem.message for problem in discovery.problems)


def test_discovery_does_not_treat_foreign_windows_absolute_paths_as_relative_on_macos(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    write_text(tmp_path / "Main.dpr", "program Main; begin end.")
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <DCC_IncludePath>C:\\External\\Library;include</DCC_IncludePath>
          </PropertyGroup>
        </Project>
        """,
    )
    (tmp_path / "include").mkdir()

    discovery = discover_delphi_project(tmp_path, project_file=tmp_path / "Main.dpr")

    assert str((tmp_path / "include").resolve()) in discovery.include_paths
    assert not any("C:" in path for path in discovery.include_paths)
    assert any(problem.kind == "external_path" and "C:\\External\\Library" in problem.message for problem in discovery.problems)


def test_lsp_workspace_config_auto_discovers_project_paths(tmp_path: Path) -> None:
    write_text(tmp_path / "Main.dpr", "program Main; uses UnitA in 'src/UnitA.pas'; begin end.")
    write_text(
        tmp_path / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <DCC_UnitSearchPath>src</DCC_UnitSearchPath>
            <DCC_IncludePath>include</DCC_IncludePath>
            <DCC_Define>MSWINDOWS</DCC_Define>
          </PropertyGroup>
        </Project>
        """,
    )
    write_text(tmp_path / "src" / "UnitA.pas", "unit UnitA; interface implementation end.")
    (tmp_path / "include").mkdir()

    state = LspWorkspaceState()
    state.configure(WorkspaceConfig(roots=[str(tmp_path)]))

    assert str((tmp_path / "src").resolve()) in state.config.search_paths
    assert str((tmp_path / "include").resolve()) in state.config.include_paths
    assert "MSWINDOWS" in state.config.defines
