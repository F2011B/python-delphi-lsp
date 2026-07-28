from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
import os
import re

from .preprocessor import IncludeLoader
from .source_reader import read_source_text

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib


CONFIG_FILE_NAME = ".delphi-lsp.toml"
_PROJECT_KEYS = frozenset({"include", "exclude"})
_WORKSPACE_KEYS = frozenset({"exclude"})
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")


class ProjectConfigError(ValueError):
    """Raised when repository project selection configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectPathConfig:
    root: Path
    source: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    workspace_exclude: tuple[str, ...] = ()

    @property
    def has_filters(self) -> bool:
        return bool(self.include or self.exclude)

    def selects(self, *paths: str | Path) -> bool:
        relative_paths = tuple(
            relative
            for path in paths
            if (relative := _repository_relative_path(path, self.root)) is not None
        )
        if not relative_paths:
            return False
        if self.include and not _matches_any(relative_paths, self.include):
            return False
        return not _matches_any(relative_paths, self.exclude)

    def excludes_workspace_path(self, path: str | Path) -> bool:
        lexical = _lexical_repository_relative_path(path, self.root)
        resolved = _repository_relative_path(path, self.root)
        if lexical is None or resolved is None:
            return True
        return _matches_any(
            (lexical, resolved),
            self.workspace_exclude,
        )


def load_project_path_config(root: str | Path) -> ProjectPathConfig | None:
    root_path = Path(root).expanduser().resolve()
    config_path = root_path / CONFIG_FILE_NAME
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"{config_path}: Invalid TOML: {exc}") from exc

    projects = _load_table(document, "projects", _PROJECT_KEYS, config_path)
    workspace = _load_table(document, "workspace", _WORKSPACE_KEYS, config_path)
    include = _load_patterns(projects, "include", config_path, section="projects")
    exclude = _load_patterns(projects, "exclude", config_path, section="projects")
    workspace_exclude = _load_patterns(
        workspace,
        "exclude",
        config_path,
        section="workspace",
    )
    return ProjectPathConfig(
        root=root_path,
        source=config_path.resolve(),
        include=include,
        exclude=exclude,
        workspace_exclude=workspace_exclude,
    )


def workspace_include_loader(
    project_config: ProjectPathConfig | None,
    include_paths: Iterable[str | Path] = (),
) -> IncludeLoader | None:
    """Build an include loader that enforces the repository's hard path boundary."""

    if project_config is None or not project_config.workspace_exclude:
        return None
    search_paths = tuple(
        Path(path).expanduser().resolve()
        for path in include_paths
    )

    def load(parent_file: str, include_name: str) -> tuple[str, str] | None:
        include_path = Path(include_name.replace("\\", "/"))
        parent = Path(parent_file).expanduser().resolve().parent
        for base in (parent, *search_paths):
            candidate = (base / include_path).resolve()
            if project_config.excludes_workspace_path(candidate):
                continue
            try:
                if candidate.is_file():
                    return read_source_text(candidate), str(candidate)
            except (OSError, UnicodeError):
                continue
        return None

    return load


def _load_table(
    document: Mapping[str, Any],
    name: str,
    supported_keys: frozenset[str],
    config_path: Path,
) -> Mapping[str, Any]:
    table = document.get(name, {})
    if not isinstance(table, Mapping):
        raise ProjectConfigError(f"{config_path}: [{name}] must be a TOML table.")
    unknown = sorted(set(table) - supported_keys)
    if unknown:
        rendered = ", ".join(str(key) for key in unknown)
        raise ProjectConfigError(
            f"{config_path}: Unsupported key in [{name}]: {rendered}."
        )
    return table


def _load_patterns(
    projects: Mapping[str, Any],
    name: str,
    config_path: Path,
    *,
    section: str,
) -> tuple[str, ...]:
    values = projects.get(name, [])
    if not isinstance(values, list):
        raise ProjectConfigError(
            f"{config_path}: {section}.{name} must be an array of strings."
        )
    if any(not isinstance(value, str) for value in values):
        raise ProjectConfigError(
            f"{config_path}: {section}.{name} must contain only strings."
        )
    normalized = tuple(
        _normalize_pattern(
            value,
            config_path=config_path,
            setting=f"{section}.{name}",
        )
        for value in values
    )
    return tuple(dict.fromkeys(normalized))


def _normalize_pattern(value: str, *, config_path: Path, setting: str) -> str:
    pattern = value.strip().replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.rstrip("/")
    if not pattern:
        raise ProjectConfigError(
            f"{config_path}: {setting} patterns must not be empty."
        )
    if pattern.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(pattern):
        raise ProjectConfigError(
            f"{config_path}: {setting} patterns must be repository-relative."
        )
    if any(part == ".." for part in pattern.split("/")):
        raise ProjectConfigError(
            f"{config_path}: {setting} patterns must not contain '..'."
        )
    return pattern


def _repository_relative_path(path: str | Path, root: Path) -> str | None:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _lexical_repository_relative_path(
    path: str | Path,
    root: Path,
) -> str | None:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        absolute = Path(os.path.abspath(os.path.normpath(candidate)))
        relative = absolute.relative_to(root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _matches_any(paths: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    return any(
        _path_matches_pattern(path.casefold(), pattern.casefold())
        for path in paths
        for pattern in patterns
    )


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if not any(token in pattern for token in ("*", "?")):
        return path == pattern or path.startswith(f"{pattern}/")
    if pattern.endswith("/**"):
        directory_pattern = pattern[:-3].rstrip("/")
        if _compile_pattern(directory_pattern).fullmatch(path):
            return True
    return bool(_compile_pattern(pattern).fullmatch(path))


@lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    translated: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            translated.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            translated.append(".*")
            index += 2
            continue
        character = pattern[index]
        if character == "*":
            translated.append("[^/]*")
        elif character == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(character))
        index += 1
    return re.compile("".join(translated))


__all__ = [
    "CONFIG_FILE_NAME",
    "ProjectConfigError",
    "ProjectPathConfig",
    "load_project_path_config",
    "workspace_include_loader",
]
