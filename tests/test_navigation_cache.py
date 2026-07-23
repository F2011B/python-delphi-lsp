from __future__ import annotations

import os
from pathlib import Path

import pytest

from delphi_lsp.navigation_cache import (
    NavigationShardStore,
    navigation_cache_key,
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink hardening")
def test_navigation_cache_does_not_follow_a_shard_directory_symlink(
    tmp_path: Path,
) -> None:
    cache_key = navigation_cache_key("unit Safe; end.", ())
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    shard_directory = root / cache_key[:2]
    shard_directory.parent.mkdir()
    shard_directory.symlink_to(outside, target_is_directory=True)
    shard = outside / f"{cache_key}.json"
    shard.write_text(
        (
            '{"cache_key":"'
            + cache_key
            + '","schema":1,"symbols":[],"lines_processed":1,"unit_name":"Safe"}'
        ),
        encoding="utf-8",
    )
    shard.chmod(0o600)

    assert NavigationShardStore(root).load(cache_key) is None


def test_navigation_cache_rejects_non_digest_keys(tmp_path: Path) -> None:
    store = NavigationShardStore(tmp_path / "cache")

    with pytest.raises(ValueError, match="malformed"):
        store.load("../outside")
