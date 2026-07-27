from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib


CONFIG_FILE_NAME = ".delphi-lsp.toml"
_PROJECT_KEYS = frozenset({"include", "exclude"})
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")


class ProjectConfigError(ValueError):
    """Raised when repository project selection configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectPathConfig:
    root: Path
    source: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

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

    projects = document.get("projects")
    if projects is None:
        return ProjectPathConfig(root=root_path, source=config_path.resolve())
    if not isinstance(projects, Mapping):
        raise ProjectConfigError(f"{config_path}: [projects] must be a TOML table.")
    unknown = sorted(set(projects) - _PROJECT_KEYS)
    if unknown:
        rendered = ", ".join(str(key) for key in unknown)
        raise ProjectConfigError(
            f"{config_path}: Unsupported key in [projects]: {rendered}."
        )
    include = _load_patterns(projects, "include", config_path)
    exclude = _load_patterns(projects, "exclude", config_path)
    return ProjectPathConfig(
        root=root_path,
        source=config_path.resolve(),
        include=include,
        exclude=exclude,
    )


def _load_patterns(
    projects: Mapping[str, Any],
    name: str,
    config_path: Path,
) -> tuple[str, ...]:
    values = projects.get(name, [])
    if not isinstance(values, list):
        raise ProjectConfigError(
            f"{config_path}: projects.{name} must be an array of strings."
        )
    if any(not isinstance(value, str) for value in values):
        raise ProjectConfigError(
            f"{config_path}: projects.{name} must contain only strings."
        )
    normalized = tuple(
        _normalize_pattern(value, config_path=config_path, setting=name)
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
            f"{config_path}: projects.{setting} patterns must not be empty."
        )
    if pattern.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(pattern):
        raise ProjectConfigError(
            f"{config_path}: projects.{setting} patterns must be repository-relative."
        )
    if any(part == ".." for part in pattern.split("/")):
        raise ProjectConfigError(
            f"{config_path}: projects.{setting} patterns must not contain '..'."
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


def _matches_any(paths: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    return any(
        _path_matches_pattern(path.casefold(), pattern.casefold())
        for path in paths
        for pattern in patterns
    )


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if not any(token in pattern for token in ("*", "?")):
        return path == pattern or path.startswith(f"{pattern}/")
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
]
