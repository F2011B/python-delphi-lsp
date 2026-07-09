from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
import os
import re
import xml.etree.ElementTree as ET


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
    include_paths: Iterable[str | os.PathLike[str]] = (),
    search_paths: Iterable[str | os.PathLike[str]] = (),
    defines: Iterable[str] = (),
    scan_workspace_sources: bool = True,
) -> DelphiProjectDiscovery:
    root_path = Path(root).expanduser().resolve()
    project_path = Path(project_file).expanduser().resolve() if project_file is not None else None
    discovery = DelphiProjectDiscovery(root=str(root_path))

    seen_search: set[str] = set()
    seen_include: set[str] = set()
    seen_defines: set[str] = set()
    seen_projects: set[str] = set()
    seen_configs: set[str] = set()

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

    candidates = _project_candidates(root_path, project_path)
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
        dproj = project.with_suffix(".dproj")
        if dproj.exists():
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
            seen_configs.add(str(dproj).casefold())
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
        populate_workspace_sources(discovery)

    return discovery


def populate_workspace_sources(discovery: DelphiProjectDiscovery) -> DelphiProjectDiscovery:
    root_path = Path(discovery.root).expanduser().resolve()
    seen_sources = {source.casefold() for source in discovery.source_files}
    seen_search = {path.casefold() for path in discovery.search_paths}
    seen_include = {path.casefold() for path in discovery.include_paths}

    _scan_sources(root_path, discovery, seen_sources)
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


def discover_workspace_sources(root: str | os.PathLike[str]) -> DelphiProjectDiscovery:
    root_path = Path(root).expanduser().resolve()
    discovery = DelphiProjectDiscovery(root=str(root_path))
    return populate_workspace_sources(discovery)


def _project_candidates(root: Path, explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit]
    candidates: list[Path] = []
    for ext in PROJECT_EXTENSIONS:
        candidates.extend(_walk_sources(root, f"*{ext}"))
    dproj_mains: list[Path] = []
    for dproj in _walk_sources(root, "*.dproj"):
        main = _main_source_from_dproj(dproj)
        if main is not None:
            dproj_mains.append((dproj.parent / main).resolve())
    for candidate in [*dproj_mains, *candidates]:
        if candidate.exists() and candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)
    return sorted(candidates, key=lambda path: str(path).casefold())


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


def _scan_sources(root: Path, discovery: DelphiProjectDiscovery, seen_sources: set[str]) -> None:
    for path in root.rglob("*"):
        if path.suffix.casefold() not in SOURCE_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        resolved = path.resolve()
        key = str(resolved).casefold()
        if key in seen_sources:
            continue
        seen_sources.add(key)
        discovery.source_files.append(str(resolved))
    discovery.source_files.sort(key=lambda item: (item.casefold(), item))


def _walk_sources(root: Path, pattern: str) -> list[Path]:
    results: list[Path] = []
    for path in root.rglob(pattern):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            results.append(path.resolve())
    return results


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
