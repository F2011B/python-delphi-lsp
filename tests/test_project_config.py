from __future__ import annotations

from pathlib import Path

import pytest

from delphi_lsp.project_config import (
    ProjectConfigError,
    load_project_path_config,
    workspace_include_loader,
)
from delphi_lsp.project_indexer import ProjectIndexer


def _write_config(root: Path, text: str) -> Path:
    path = root / ".delphi-lsp.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_and_matches_repository_relative_project_paths(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [projects]
        include = ["Apps/**", "Services/Api/ApiServer.dproj"]
        exclude = ["**/Examples/**", "Apps/Legacy"]
        """,
    )

    config = load_project_path_config(tmp_path)

    assert config is not None
    assert config.source == config_path.resolve()
    assert config.include == ("Apps/**", "Services/Api/ApiServer.dproj")
    assert config.exclude == ("**/Examples/**", "Apps/Legacy")
    assert config.has_filters
    assert config.selects(
        tmp_path / "apps" / "Desktop" / "Client.dpr",
        tmp_path / "apps" / "Desktop" / "Client.dproj",
    )
    assert config.selects(
        tmp_path / "services" / "Api" / "ApiServer.dpr",
        tmp_path / "services" / "Api" / "ApiServer.dproj",
    )
    assert not config.selects(
        tmp_path / "apps" / "examples" / "Demo.dpr",
        tmp_path / "apps" / "examples" / "Demo.dproj",
    )
    assert not config.selects(
        tmp_path / "Apps" / "Legacy" / "OldApp.dpr",
        tmp_path / "Apps" / "Legacy" / "OldApp.dproj",
    )
    assert not config.selects(
        tmp_path / "tools" / "Generator.dpr",
        tmp_path / "tools" / "Generator.dproj",
    )


def test_globs_distinguish_single_and_recursive_segments(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
        [projects]
        include = ["apps/*/Main.dpr", "services/**/Server.dpr"]
        """,
    )
    config = load_project_path_config(tmp_path)
    assert config is not None

    assert config.selects(tmp_path / "apps" / "one" / "Main.dpr")
    assert not config.selects(tmp_path / "apps" / "one" / "two" / "Main.dpr")
    assert config.selects(tmp_path / "services" / "Server.dpr")
    assert config.selects(tmp_path / "services" / "api" / "v2" / "Server.dpr")


def test_missing_configuration_is_not_an_active_filter(tmp_path: Path) -> None:
    assert load_project_path_config(tmp_path) is None


def test_workspace_exclude_matches_complete_directories_and_files(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        """
        [workspace]
        exclude = ["vendor", "generated/**", "**/temp/**"]
        """,
    )

    config = load_project_path_config(tmp_path)

    assert config is not None
    assert config.workspace_exclude == ("vendor", "generated/**", "**/temp/**")
    assert config.excludes_workspace_path(tmp_path / "vendor")
    assert config.excludes_workspace_path(tmp_path / "vendor" / "UnitA.pas")
    assert config.excludes_workspace_path(tmp_path / "generated")
    assert config.excludes_workspace_path(tmp_path / "generated" / "v2" / "Api.pas")
    assert config.excludes_workspace_path(tmp_path / "apps" / "temp")
    assert config.excludes_workspace_path(tmp_path / "apps" / "temp" / "Cache.pas")
    assert not config.excludes_workspace_path(tmp_path / "src" / "UnitA.pas")


def test_workspace_exclude_fails_closed_for_escaping_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _write_config(
        root,
        """
        [workspace]
        exclude = ["vendor"]
        """,
    )
    (outside / "Lib.pas").write_text(
        "unit Lib; interface implementation end.\n",
        encoding="utf-8",
    )
    (outside / "Shared.inc").write_text("const Shared = 1;\n", encoding="utf-8")
    try:
        (root / "vendor").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    config = load_project_path_config(root)
    assert config is not None
    assert config.excludes_workspace_path(root / "vendor" / "Lib.pas")
    assert not ProjectIndexer(project_config=config)._source_is_allowed(
        outside / "Lib.pas"
    )
    include_loader = workspace_include_loader(config, [root / "vendor"])
    assert include_loader is not None
    assert include_loader(str(root / "Main.pas"), "Shared.inc") is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[projects\n", "Invalid TOML"),
        ("[projects]\nunknown = []\n", "Unsupported key"),
        ("[projects]\ninclude = 'apps'\n", "must be an array"),
        ("[projects]\nexclude = [1]\n", "must contain only strings"),
        ("[projects]\ninclude = ['/absolute/**']\n", "repository-relative"),
        ("[projects]\ninclude = ['../outside/**']\n", "must not contain '..'"),
        ("[projects]\ninclude = ['C:/outside/**']\n", "repository-relative"),
        ("[projects]\ninclude = ['']\n", "must not be empty"),
        ("[workspace]\nunknown = []\n", "Unsupported key"),
        ("[workspace]\nexclude = 'vendor'\n", "must be an array"),
        ("[workspace]\nexclude = ['../vendor']\n", "must not contain '..'"),
    ],
)
def test_rejects_invalid_project_configuration(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    config_path = _write_config(tmp_path, text)

    with pytest.raises(ProjectConfigError, match=message) as raised:
        load_project_path_config(tmp_path)

    assert str(config_path) in str(raised.value)
