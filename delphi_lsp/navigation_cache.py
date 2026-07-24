from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping


NAVIGATION_SHARD_SCHEMA = 1
_MAX_SHARD_BYTES = 256 * 1024**2
_KEY_PREFIX = b"python-delphi-lsp-navigation-shard-v1\0"


def navigation_cache_key(text: str, defines: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(_KEY_PREFIX)
    digest.update(
        json.dumps(
            sorted((define.casefold() for define in defines)),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\0")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


class NavigationShardStore:
    """Content-addressed JSON storage for detached navigation symbols."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load(self, cache_key: str) -> Mapping[str, object] | None:
        path = self._path(cache_key)
        if self.root.is_symlink() or path.parent.is_symlink():
            return None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > _MAX_SHARD_BYTES
                    or (
                        os.name != "nt"
                        and (
                            info.st_uid != os.getuid()
                            or info.st_mode & 0o077
                        )
                    )
                ):
                    return None
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_SHARD_BYTES + 1)
                if len(raw) > _MAX_SHARD_BYTES:
                    return None
            finally:
                os.close(descriptor)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != NAVIGATION_SHARD_SCHEMA
            or payload.get("cache_key") != cache_key
            or not isinstance(payload.get("symbols"), list)
        ):
            return None
        return payload

    def store(self, cache_key: str, payload: Mapping[str, object]) -> None:
        path = self._path(cache_key)
        self._ensure_directory(path.parent)
        record = {
            **dict(payload),
            "schema": NAVIGATION_SHARD_SCHEMA,
            "cache_key": cache_key,
        }
        data = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > _MAX_SHARD_BYTES:
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".navigation-", dir=path.parent)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _path(self, cache_key: str) -> Path:
        if (
            len(cache_key) != 64
            or any(character not in "0123456789abcdef" for character in cache_key)
        ):
            raise ValueError("navigation cache key is malformed")
        return self.root / cache_key[:2] / f"{cache_key}.json"

    def _ensure_directory(self, directory: Path) -> None:
        for path in (self.root, directory):
            if path.exists() and path.is_symlink():
                raise OSError("navigation cache path is unsafe")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(path, 0o700)


__all__ = [
    "NAVIGATION_SHARD_SCHEMA",
    "NavigationShardStore",
    "navigation_cache_key",
]
