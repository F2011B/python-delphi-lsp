from __future__ import annotations

import errno
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
import os
import re
import xml.etree.ElementTree as ET

from .progress import ProgressCallback, ProgressEvent
from .project_config import (
    ProjectConfigError,
    ProjectPathConfig,
    load_project_path_config,
)


SOURCE_EXTENSIONS = (".pas", ".dpr", ".dpk", ".inc")
PROJECT_EXTENSIONS = (".dpr", ".dpk")
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".worktrees",
    "node_modules",
}


@dataclass(frozen=True)
class DiscoveryProblem:
    kind: str
    message: str
    origin: str


@dataclass
class DelphiProjectDiscovery:
    root: str
    project_files: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    search_paths: list[str] = field(default_factory=list)
    include_paths: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    unit_paths: dict[str, list[str]] = field(default_factory=dict)
    problems: list[DiscoveryProblem] = field(default_factory=list)
    search_path_origins: dict[str, list[str]] = field(default_factory=dict)
    include_path_origins: dict[str, list[str]] = field(default_factory=dict)
    define_origins: dict[str, list[str]] = field(default_factory=dict)
    project_config: ProjectPathConfig | None = None


_DPR_UNIT_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\b\s*(?:in\s*['\"](?P<path>[^'\"]+)['\"])?",
    re.IGNORECASE,
)
_DPR_CLAUSE_RE = re.compile(r"\b(?:uses|contains)\b(?P<body>.*?);", re.IGNORECASE | re.DOTALL)
_CFG_TOKEN_RE = re.compile(r"(?P<option>-[UID])(?P<value>.+)", re.IGNORECASE)
_MACRO_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def discover_delphi_project(
    root: str | os.PathLike[str],
    *,
    project_file: str | os.PathLike[str] | None = None,
    main_projects_only: bool = False,
    include_paths: Iterable[str | os.PathLike[str]] = (),
    search_paths: Iterable[str | os.PathLike[str]] = (),
    defines: Iterable[str] = (),
    scan_workspace_sources: bool = True,
    on_progress: ProgressCallback | None = None,
) -> DelphiProjectDiscovery:
    root_path = Path(root).expanduser().resolve()
    project_config = load_project_path_config(root_path)
    project_path = Path(project_file).expanduser() if project_file is not None else None
    if project_path is not None:
        if not project_path.is_absolute():
            project_path = root_path / project_path
        project_path = project_path.resolve()
        if (
            project_config is not None
            and project_config.excludes_workspace_path(project_path)
        ):
            raise ProjectConfigError(
                f"{project_config.source}: explicit project {project_path} is "
                "excluded by workspace.exclude."
            )
    discovery = DelphiProjectDiscovery(
        root=str(root_path),
        project_config=project_config,
    )
    _emit_progress(on_progress, "discovery", str(root_path), 0, 0, None, "project discovery started")

    seen_search: set[str] = set()
    seen_include: set[str] = set()
    seen_defines: set[str] = set()
    seen_projects: set[str] = set()
    seen_configs: set[str] = set()
    project_configs: dict[str, tuple[Path, ...]] = {}
    if project_config is not None:
        config_key = str(project_config.source).casefold()
        seen_configs.add(config_key)
        discovery.config_files.append(str(project_config.source))

    def add_path(
        target: list[str],
        seen: set[str],
        origins: dict[str, list[str]],
        path: Path | str,
        *,
        base: Path,
        origin: str,
    ) -> None:
        _add_resolved_path(
            target,
            seen,
            origins,
            str(path),
            base=base,
            origin=origin,
            discovery=discovery,
        )

    def add_define(raw: str, *, origin: str = "manual define") -> None:
        for item in _split_list(raw):
            define = item.strip()
            if not define:
                continue
            if _MACRO_RE.search(define):
                discovery.problems.append(
                    DiscoveryProblem("unresolved_macro", f"Could not resolve {define} in define list", origin)
                )
                continue
            key = define.casefold()
            if key not in seen_defines:
                seen_defines.add(key)
                discovery.defines.append(define)
            exposed_define = next(item for item in discovery.defines if item.casefold() == key)
            _record_origin(discovery.define_origins, exposed_define, origin)

    for value in search_paths:
        add_path(
            discovery.search_paths,
            seen_search,
            discovery.search_path_origins,
            Path(value),
            base=root_path,
            origin="manual search path",
        )
    for value in include_paths:
        add_path(
            discovery.include_paths,
            seen_include,
            discovery.include_path_origins,
            Path(value),
            base=root_path,
            origin="manual include path",
        )
    for value in defines:
        add_define(value)

    explicit_dproj: Path | None = None
    if (
        project_path is not None
        and project_path.suffix.casefold() == ".dproj"
    ):
        explicit_dproj = project_path
        project_path = _project_entry_from_dproj(
            explicit_dproj,
            discovery,
        )
        if (
            project_path is not None
            and project_config is not None
            and project_config.excludes_workspace_path(project_path)
        ):
            raise ProjectConfigError(
                f"{project_config.source}: explicit project {project_path} is "
                "excluded by workspace.exclude."
            )
    candidates = (
        []
        if explicit_dproj is not None and project_path is None
        else _project_candidates(
            root_path,
            project_path,
            main_projects_only=main_projects_only,
            project_config=project_config,
            project_configs=project_configs,
        )
    )
    for project in candidates:
        key = str(project).casefold()
        if key not in seen_projects:
            seen_projects.add(key)
            discovery.project_files.append(str(project))
        if project.suffix.casefold() in PROJECT_EXTENSIONS:
            _read_dpr_paths(
                project,
                discovery,
                discovery.search_paths,
                seen_search,
                discovery.search_path_origins,
            )
        dproj_candidates = [
            *project_configs.get(str(project).casefold(), ()),
            project.with_suffix(".dproj"),
        ]
        if explicit_dproj is not None:
            dproj_candidates.insert(0, explicit_dproj)
        for dproj in dproj_candidates:
            config_key = str(dproj).casefold()
            if config_key in seen_configs or not dproj.exists():
                continue
            _read_dproj(
                dproj,
                discovery,
                discovery.search_paths,
                seen_search,
                discovery.search_path_origins,
                discovery.include_paths,
                seen_include,
                discovery.include_path_origins,
                add_define,
            )
            seen_configs.add(config_key)
            discovery.config_files.append(str(dproj))
        for cfg in (project.with_suffix(".cfg"), project.with_suffix(".dof")):
            if cfg.exists():
                _read_cfg(
                    cfg,
                    discovery,
                    discovery.search_paths,
                    seen_search,
                    discovery.search_path_origins,
                    discovery.include_paths,
                    seen_include,
                    discovery.include_path_origins,
                    add_define,
                )
                key = str(cfg).casefold()
                if key not in seen_configs:
                    seen_configs.add(key)
                    discovery.config_files.append(str(cfg))

    if scan_workspace_sources:
        populate_workspace_sources(discovery, on_progress=on_progress)

    _emit_progress(
        on_progress,
        "inventory",
        str(root_path),
        len(discovery.source_files),
        0,
        len(discovery.source_files),
        "project discovery complete",
    )

    return discovery


def populate_workspace_sources(
    discovery: DelphiProjectDiscovery,
    *,
    on_progress: ProgressCallback | None = None,
) -> DelphiProjectDiscovery:
    root_path = Path(discovery.root).expanduser().resolve()
    seen_sources = {source.casefold() for source in discovery.source_files}
    seen_search = {path.casefold() for path in discovery.search_paths}
    seen_include = {path.casefold() for path in discovery.include_paths}

    _scan_sources(root_path, discovery, seen_sources, on_progress=on_progress)
    total_sources = len(discovery.source_files)
    _emit_progress(
        on_progress,
        "inventory",
        str(root_path),
        total_sources,
        0,
        total_sources,
        "workspace source inventory complete",
    )
    for source in discovery.source_files:
        path = Path(source)
        unit_key = path.stem.casefold()
        unit_paths = discovery.unit_paths.setdefault(unit_key, [])
        if source not in unit_paths:
            unit_paths.append(source)
        if path.suffix.casefold() in {".pas", ".dpr", ".dpk"}:
            _add_resolved_path(
                discovery.search_paths,
                seen_search,
                discovery.search_path_origins,
                str(path.parent),
                base=root_path,
                origin="workspace source scan",
                discovery=discovery,
            )
        elif path.suffix.casefold() == ".inc":
            _add_resolved_path(
                discovery.include_paths,
                seen_include,
                discovery.include_path_origins,
                str(path.parent),
                base=root_path,
                origin="workspace include scan",
                discovery=discovery,
            )

    return discovery


def discover_workspace_sources(
    root: str | os.PathLike[str],
    *,
    on_progress: ProgressCallback | None = None,
) -> DelphiProjectDiscovery:
    root_path = Path(root).expanduser().resolve()
    project_config = load_project_path_config(root_path)
    discovery = DelphiProjectDiscovery(
        root=str(root_path),
        project_config=project_config,
    )
    if project_config is not None:
        discovery.config_files.append(str(project_config.source))
    _emit_progress(on_progress, "discovery", str(root_path), 0, 0, None, "workspace discovery started")
    populate_workspace_sources(discovery, on_progress=on_progress)
    _emit_progress(
        on_progress,
        "complete",
        str(root_path),
        len(discovery.source_files),
        len(discovery.source_files),
        len(discovery.source_files),
        "workspace discovery complete",
    )
    return discovery


def _emit_progress(
    callback: ProgressCallback | None,
    phase: str,
    path: str,
    files_discovered: int,
    files_completed: int,
    files_total: int | None,
    detail: str,
) -> None:
    if callback is not None:
        callback(
            ProgressEvent(
                phase=phase,
                language="delphi",
                path=path,
                files_discovered=files_discovered,
                files_completed=files_completed,
                files_total=files_total,
                lines_processed=0,
                symbols_discovered=0,
                cached_files=0,
                detail=detail,
            )
        )


def _project_candidates(
    root: Path,
    explicit: Path | None,
    *,
    main_projects_only: bool = False,
    project_config: ProjectPathConfig | None = None,
    project_configs: dict[str, tuple[Path, ...]] | None = None,
) -> list[Path]:
    if explicit is not None:
        return [explicit]
    candidates: list[Path] = []
    aliases: dict[str, list[Path]] = {}
    configs_by_entry: dict[str, list[Path]] = {}
    for ext in PROJECT_EXTENSIONS:
        candidates.extend(_walk_sources(root, f"*{ext}", project_config))
    for candidate in candidates:
        key = str(candidate).casefold()
        aliases.setdefault(key, []).append(candidate)
        companion = candidate.with_suffix(".dproj")
        if companion.is_file():
            aliases[key].append(companion)
            configs_by_entry.setdefault(key, []).append(companion)
    for dproj in _walk_sources(root, "*.dproj", project_config):
        main = _main_source_from_dproj(dproj)
        if main is not None:
            entry = (dproj.parent / main).resolve()
            key = str(entry).casefold()
            aliases.setdefault(key, []).extend((entry, dproj))
            configs_by_entry.setdefault(key, []).append(dproj)
            candidates.append(entry)
    candidates = sorted(
        {
            candidate
            for candidate in candidates
            if candidate.exists()
            and candidate.is_file()
            and (
                project_config is None
                or not project_config.excludes_workspace_path(candidate)
            )
        },
        key=lambda path: str(path).casefold(),
    )
    if project_config is not None and project_config.has_filters:
        candidates = [
            candidate
            for candidate in candidates
            if project_config.selects(
                *aliases.get(
                    str(candidate).casefold(),
                    (candidate, candidate.with_suffix(".dproj")),
                )
            )
        ]
        if not candidates:
            raise ProjectConfigError(
                f"{project_config.source}: configured [projects] filters matched "
                "no Delphi projects."
            )
        _set_project_configs(project_configs, configs_by_entry, candidates)
        return candidates
    if not main_projects_only or not candidates:
        _set_project_configs(project_configs, configs_by_entry, candidates)
        return candidates

    primary = [
        candidate
        for candidate in candidates
        if not _is_auxiliary_project(candidate, root)
    ]
    configured = [
        candidate
        for candidate in primary
        if candidate.with_suffix(".dproj").is_file()
    ]
    root_entries = [
        candidate
        for candidate in primary
        if candidate.parent == root
    ]
    scoped = root_entries or configured
    if not scoped:
        return []
    minimum_depth = min(
        len(candidate.parent.relative_to(root).parts)
        for candidate in scoped
    )
    selected = [
        candidate
        for candidate in scoped
        if len(candidate.parent.relative_to(root).parts) == minimum_depth
    ]
    _set_project_configs(project_configs, configs_by_entry, selected)
    return selected


def _set_project_configs(
    target: dict[str, tuple[Path, ...]] | None,
    configs_by_entry: dict[str, list[Path]],
    selected: list[Path],
) -> None:
    if target is None:
        return
    for candidate in selected:
        key = str(candidate).casefold()
        configs = sorted(
            set(configs_by_entry.get(key, ())),
            key=lambda path: str(path).casefold(),
        )
        if configs:
            target[key] = tuple(configs)


_AUXILIARY_PROJECT_PARTS = {
    "benchmark",
    "benchmarks",
    "demo",
    "demos",
    "ex",
    "example",
    "examples",
    "sample",
    "samples",
    "test",
    "tests",
    "testing",
    "thirdparty",
    "third-party",
    "vendor",
    "vendors",
}


def _is_auxiliary_project(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    directory_parts = {part.casefold() for part in relative.parent.parts}
    if directory_parts & _AUXILIARY_PROJECT_PARTS:
        return True
    stem = path.stem.casefold()
    return (
        "example" in stem
        or "sample" in stem
        or stem.startswith("test")
        or stem.endswith("tests")
    )


def _read_dpr_paths(
    project: Path,
    discovery: DelphiProjectDiscovery,
    search_paths: list[str],
    seen_search: set[str],
    search_path_origins: dict[str, list[str]],
) -> None:
    try:
        text = project.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = project.read_text(encoding="latin-1")
    except OSError as exc:
        discovery.problems.append(DiscoveryProblem("cant_read_project", str(exc), str(project)))
        return
    for clause in _DPR_CLAUSE_RE.finditer(text):
        body = clause.group("body")
        for match in _DPR_UNIT_RE.finditer(body):
            unit_path = match.group("path")
            if not unit_path:
                continue
            resolved = _resolve_project_path(unit_path, base=project.parent, origin=str(project), discovery=discovery)
            if resolved is None:
                continue
            _add_resolved_path(
                search_paths,
                seen_search,
                search_path_origins,
                str(resolved.parent),
                base=project.parent,
                origin=str(project),
                discovery=discovery,
            )


def _read_dproj(
    path: Path,
    discovery: DelphiProjectDiscovery,
    search_paths: list[str],
    seen_search: set[str],
    search_path_origins: dict[str, list[str]],
    include_paths: list[str],
    seen_include: set[str],
    include_path_origins: dict[str, list[str]],
    add_define,
) -> None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        discovery.problems.append(DiscoveryProblem("cant_read_dproj", str(exc), str(path)))
        return

    for element in root.iter():
        name = _xml_local_name(element.tag)
        text = (element.text or "").strip()
        if name in {"DCC_UnitSearchPath", "UnitSearchPath"} and text:
            for item in _split_list(text):
                _add_resolved_path(
                    search_paths,
                    seen_search,
                    search_path_origins,
                    item,
                    base=path.parent,
                    origin=str(path),
                    discovery=discovery,
                )
        elif name in {"DCC_IncludePath", "IncludePath"} and text:
            for item in _split_list(text):
                _add_resolved_path(
                    include_paths,
                    seen_include,
                    include_path_origins,
                    item,
                    base=path.parent,
                    origin=str(path),
                    discovery=discovery,
                )
        elif name in {"DCC_Define", "DefineConstants"} and text:
                add_define(text, origin=str(path))

        if name == "DCCReference":
            include = element.attrib.get("Include") or element.attrib.get("include")
            if include:
                resolved = _resolve_project_path(include, base=path.parent, origin=str(path), discovery=discovery)
                if resolved is not None:
                    _add_resolved_path(
                        search_paths,
                        seen_search,
                        search_path_origins,
                        str(resolved.parent),
                        base=path.parent,
                        origin=str(path),
                        discovery=discovery,
                    )


def _read_cfg(
    path: Path,
    discovery: DelphiProjectDiscovery,
    search_paths: list[str],
    seen_search: set[str],
    search_path_origins: dict[str, list[str]],
    include_paths: list[str],
    seen_include: set[str],
    include_path_origins: dict[str, list[str]],
    add_define,
) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="latin-1").splitlines()
    except OSError as exc:
        discovery.problems.append(DiscoveryProblem("cant_read_config", str(exc), str(path)))
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _CFG_TOKEN_RE.match(stripped)
        if match is None:
            continue
        option = match.group("option").casefold()
        value = match.group("value")
        if option == "-u":
            for item in _split_list(value):
                _add_resolved_path(
                    search_paths,
                    seen_search,
                    search_path_origins,
                    item,
                    base=path.parent,
                    origin=str(path),
                    discovery=discovery,
                )
        elif option == "-i":
            for item in _split_list(value):
                _add_resolved_path(
                    include_paths,
                    seen_include,
                    include_path_origins,
                    item,
                    base=path.parent,
                    origin=str(path),
                    discovery=discovery,
                )
        elif option == "-d":
            add_define(value, origin=str(path))


def _main_source_from_dproj(path: Path) -> str | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    for element in root.iter():
        if _xml_local_name(element.tag) == "MainSource" and element.text:
            return element.text.strip()
    return None


def _project_entry_from_dproj(
    path: Path,
    discovery: DelphiProjectDiscovery,
) -> Path | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        discovery.problems.append(
            DiscoveryProblem(
                "cant_read_project",
                f"Could not parse Delphi project file: {exc}",
                str(path),
            )
        )
        return None
    except OSError as exc:
        discovery.problems.append(
            DiscoveryProblem(
                "cant_read_project",
                f"Could not read Delphi project file: {exc}",
                str(path),
            )
        )
        return None

    main_source = next(
        (
            (element.text or "").strip()
            for element in root.iter()
            if _xml_local_name(element.tag) == "MainSource"
            and (element.text or "").strip()
        ),
        "",
    )
    if not main_source:
        discovery.problems.append(
            DiscoveryProblem(
                "cant_read_project",
                "Delphi project file does not define MainSource.",
                str(path),
            )
        )
        return None

    entry = (path.parent / main_source).resolve()
    if entry.suffix.casefold() not in PROJECT_EXTENSIONS:
        discovery.problems.append(
            DiscoveryProblem(
                "cant_read_project",
                "MainSource must reference a .dpr or .dpk entry file.",
                str(path),
            )
        )
        return None
    if not entry.is_file():
        discovery.problems.append(
            DiscoveryProblem(
                "cant_read_project",
                f"MainSource does not exist: {entry}",
                str(path),
            )
        )
        return None
    return entry


def _scan_sources(
    root: Path,
    discovery: DelphiProjectDiscovery,
    seen_sources: set[str],
    *,
    on_progress: ProgressCallback | None = None,
) -> None:
    for path in _walk_files(root, discovery.project_config):
        if path.suffix.casefold() not in SOURCE_EXTENSIONS:
            continue
        resolved = path.resolve()
        key = str(resolved).casefold()
        if key in seen_sources:
            continue
        seen_sources.add(key)
        discovery.source_files.append(str(resolved))
        _emit_progress(
            on_progress,
            "discovery",
            str(resolved),
            len(discovery.source_files),
            0,
            None,
            "source file discovered",
        )
    discovery.source_files.sort(key=lambda item: (item.casefold(), item))


def _walk_sources(
    root: Path,
    pattern: str,
    project_config: ProjectPathConfig | None = None,
) -> list[Path]:
    results: list[Path] = []
    for path in _walk_files(root, project_config):
        if path.match(pattern):
            results.append(path.resolve())
    return results


def _walk_files(
    root: Path,
    project_config: ProjectPathConfig | None = None,
) -> Iterator[Path]:
    def handle_error(error: OSError) -> None:
        if _is_path_too_long(error):
            return
        raise error

    for directory, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=handle_error,
    ):
        current = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in SKIP_DIRS
            and (
                project_config is None
                or not project_config.excludes_workspace_path(current / name)
            )
        )
        for name in sorted(file_names):
            path = current / name
            if (
                project_config is not None
                and project_config.excludes_workspace_path(path)
            ):
                continue
            try:
                if path.is_file():
                    yield path
            except OSError as error:
                if _is_path_too_long(error):
                    continue
                raise


def _is_path_too_long(error: OSError) -> bool:
    return getattr(error, "winerror", None) == 206 or error.errno == errno.ENAMETOOLONG


def _add_resolved_path(
    target: list[str],
    seen: set[str],
    origins: dict[str, list[str]],
    value: str,
    *,
    base: Path,
    origin: str,
    discovery: DelphiProjectDiscovery,
) -> None:
    resolved = _resolve_project_path(value, base=base, origin=origin, discovery=discovery)
    if resolved is None:
        return
    if (
        discovery.project_config is not None
        and discovery.project_config.excludes_workspace_path(resolved)
    ):
        return
    key = str(resolved).casefold()
    if key not in seen:
        seen.add(key)
        target.append(str(resolved))
    exposed_path = next(item for item in target if item.casefold() == key)
    _record_origin(origins, exposed_path, origin)


def _record_origin(origins: dict[str, list[str]], key: str, origin: str) -> None:
    entries = origins.setdefault(key, [])
    if origin not in entries:
        entries.append(origin)


def _resolve_project_path(
    raw: str,
    *,
    base: Path,
    origin: str,
    discovery: DelphiProjectDiscovery,
) -> Path | None:
    cleaned = raw.strip().strip('"').strip("'")
    if not cleaned:
        return None
    if os.name != "nt" and _WINDOWS_ABSOLUTE_RE.match(cleaned):
        discovery.problems.append(
            DiscoveryProblem("external_path", f"Skipping non-local Windows absolute path: {raw}", origin)
        )
        return None
    normalized = cleaned.replace("\\", os.sep)
    macros = _MACRO_RE.findall(normalized)
    replacements = {
        "PROJECTDIR": str(base),
        "PROJECT_DIR": str(base),
        "MSBUILDPROJECTDIRECTORY": str(base),
        "MSBUILDTHISFILEDIRECTORY": str(base),
    }
    for macro in macros:
        value = replacements.get(macro.upper())
        if value is None:
            discovery.problems.append(
                DiscoveryProblem("unresolved_macro", f"Could not resolve $({macro}) in {raw}", origin)
            )
            return None
        normalized = normalized.replace(f"$({macro})", value)
    normalized = os.path.expandvars(normalized)
    path = Path(normalized)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ";").split(";") if item.strip()]


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = [
    "DelphiProjectDiscovery",
    "DiscoveryProblem",
    "discover_delphi_project",
    "discover_workspace_sources",
    "populate_workspace_sources",
]
