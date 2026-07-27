from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from delphi_lsp.agent_wiki import WikiExportError, export_okf_wiki


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def _make_repository(root: Path) -> None:
    _write(
        root / "Main.dpr",
        """
        program Main;
        uses Worker in 'src/Worker.pas', Extra in 'src/Extra.pas';
        begin
          TWorker.Create.Run;
        end.
        """,
    )
    _write(
        root / "Main.dproj",
        """
        <Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
          <PropertyGroup>
            <MainSource>Main.dpr</MainSource>
            <DCC_UnitSearchPath>src</DCC_UnitSearchPath>
          </PropertyGroup>
          <ItemGroup>
            <DCCReference Include="src/Worker.pas" />
            <DCCReference Include="src/Extra.pas" />
          </ItemGroup>
        </Project>
        """,
    )
    _write(
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
          Writeln('wiki implementation body');
        end;
        end.
        """,
    )
    _write(
        root / "src" / "Extra.pas",
        """
        unit Extra;
        interface
        type
          TWorker = record
            Value: Integer;
          end;
        implementation
        end.
        """,
    )


def _concept_markdown_files(bundle: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in bundle.rglob("*.md")
            if path.name != "index.md" and path.name != "log.md"
        ),
        key=lambda path: path.relative_to(bundle).as_posix(),
    )


def _assert_internal_links_resolve(bundle: Path) -> None:
    for source in bundle.rglob("*.md"):
        text = source.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+\.md)(?:#[^)]*)?\)", text):
            if target.startswith("/"):
                resolved = bundle / target.removeprefix("/")
            else:
                resolved = source.parent / target
            assert resolved.resolve().is_file(), f"{source}: broken link {target}"


def test_export_okf_wiki_contains_all_layered_knowledge_and_valid_links(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    output = tmp_path / "knowledge"
    _make_repository(repository)

    result = export_okf_wiki(repository, output, workers=1)

    assert result.output == str(output.resolve())
    assert result.projects == 1
    assert result.units == 3
    assert result.symbols >= 4
    markdown_files = sorted(output.rglob("*.md"))
    assert result.documents == len(markdown_files)
    assert (output / "index.md").read_text(encoding="utf-8").startswith(
        '---\nokf_version: "0.2"\n---\n'
    )
    assert (output / "overview.md").is_file()
    assert (output / "projects" / "index.md").is_file()
    assert (output / "units" / "index.md").is_file()
    assert (output / "symbols" / "index.md").is_file()
    assert (output / "metrics" / "index.md").is_file()
    assert (output / "problems" / "index.md").is_file()
    assert (output / "reference" / "protocol.md").is_file()
    assert (output / "reference" / "cpg.md").is_file()
    assert (output / "reference" / "cache.md").is_file()

    concepts = _concept_markdown_files(output)
    assert concepts
    for concept in concepts:
        text = concept.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert re.search(r'^type: "[^"]+"\s*$', text, re.MULTILINE)

    symbol_pages = list((output / "symbols").glob("*.md"))
    assert len(symbol_pages) - 1 == result.symbols
    worker_pages = [
        path
        for path in symbol_pages
        if path.name != "index.md"
        and "\n# TWorker\n" in path.read_text(encoding="utf-8")
    ]
    assert len(worker_pages) == 2
    assert len({path.name.casefold() for path in worker_pages}) == 2
    assert any(
        "wiki implementation body" in path.read_text(encoding="utf-8")
        for path in symbol_pages
        if path.name != "index.md"
    )
    unit_pages = [
        path for path in (output / "units").glob("*.md") if path.name != "index.md"
    ]
    assert any(
        "](/projects/" in path.read_text(encoding="utf-8") for path in unit_pages
    )
    assert "total_loc" in (output / "metrics" / "workspace.md").read_text(encoding="utf-8")
    assert "open" in (output / "reference" / "protocol.md").read_text(encoding="utf-8")
    assert "full" in (output / "reference" / "cpg.md").read_text(encoding="utf-8")
    _assert_internal_links_resolve(output)


def test_export_is_deterministic_and_force_replaces_only_destination(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _make_repository(repository)

    export_okf_wiki(repository, first, workers=1)
    export_okf_wiki(repository, second, workers=1)
    first_files = {
        path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()
    }
    second_files = {
        path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()
    }
    assert first_files == second_files

    stale = first / "stale.txt"
    stale.write_text("must disappear", encoding="utf-8")
    with pytest.raises(WikiExportError, match="already exists"):
        export_okf_wiki(repository, first, workers=1)
    export_okf_wiki(repository, first, workers=1, force=True)
    assert not stale.exists()
    assert (repository / "Main.dpr").is_file()


def test_export_rejects_unsafe_destination(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _make_repository(repository)

    with pytest.raises(WikiExportError, match="repository root"):
        export_okf_wiki(repository, repository, force=True)


def test_export_rejects_leaf_symlink_without_touching_target(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    victim = tmp_path / "victim"
    destination = tmp_path / "wiki-link"
    _make_repository(repository)
    victim.mkdir()
    marker = victim / "KEEP.txt"
    marker.write_text("keep", encoding="utf-8")
    try:
        destination.symlink_to(victim, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(WikiExportError, match="symlink"):
        export_okf_wiki(repository, destination, workers=1, force=True)

    assert destination.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_export_resolves_symlinked_parent_without_following_leaf(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    real_parent = tmp_path / "real-parent"
    parent_link = tmp_path / "parent-link"
    _make_repository(repository)
    real_parent.mkdir()
    try:
        parent_link.symlink_to(real_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    result = export_okf_wiki(repository, parent_link / "wiki", workers=1)

    assert result.output == str((real_parent / "wiki").resolve())
    assert (real_parent / "wiki" / "index.md").is_file()


def test_export_accepts_existing_empty_destination_without_force(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    destination = tmp_path / "wiki"
    _make_repository(repository)
    destination.mkdir()

    export_okf_wiki(repository, destination, workers=1)

    assert (destination / "index.md").is_file()


def test_shared_unit_links_back_to_every_owning_project(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    output = tmp_path / "wiki"
    _make_repository(repository)
    _write(
        repository / "Second.dpr",
        """
        program Second;
        uses Worker in 'src/Worker.pas';
        begin
        end.
        """,
    )

    export_okf_wiki(repository, output, workers=1)

    worker_unit = next(
        path
        for path in (output / "units").glob("*.md")
        if "\n# Worker\n" in path.read_text(encoding="utf-8")
    )
    assert worker_unit.read_text(encoding="utf-8").count("](/projects/") == 2


def test_duplicate_unit_names_keep_distinct_metric_pages_and_links(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    output = tmp_path / "wiki"
    _write(
        repository / "one" / "Duplicate.pas",
        """
        unit Duplicate;
        interface
        type TFirst = Integer;
        implementation
        end.
        """,
    )
    _write(
        repository / "two" / "Duplicate.pas",
        """
        unit Duplicate;
        interface
        type TSecond = Integer;
        implementation
        end.
        """,
    )

    export_okf_wiki(repository, output, workers=1)

    metric_pages = [
        path
        for path in (output / "metrics" / "units").glob("*.md")
        if path.name != "index.md"
    ]
    assert len(metric_pages) == 2
    assert len({path.name.casefold() for path in metric_pages}) == 2
    _assert_internal_links_resolve(output)


def test_include_unit_without_unit_metrics_has_no_broken_metric_link(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    output = tmp_path / "wiki"
    _write(
        repository / "Shared.inc",
        """
        const
          SharedValue = 42;
        """,
    )

    export_okf_wiki(repository, output, workers=1)

    _assert_internal_links_resolve(output)


def test_agent_cli_exports_okf_wiki_and_reports_json_summary(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    output = tmp_path / "wiki"
    _make_repository(repository)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "delphi_lsp.agent_cli",
            "wiki",
            "export",
            "--root",
            str(repository),
            "--out",
            str(output),
            "--workers",
            "1",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["format"] == "okf"
    assert payload["okf_version"] == "0.2"
    assert payload["output"] == str(output.resolve())
    assert payload["documents"] == len(list(output.rglob("*.md")))
    assert completed.stderr == ""
