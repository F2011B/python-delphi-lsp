#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


Downloader = Callable[..., str]
DEFAULT_HF_HOME = Path('/Volumes/MacDataSSDPro/.cache/huggingface')
DEFAULT_REPO_ID = 'deepreinforce-ai/Ornith-1.0-9B'


def _load_cache_checker():
    checker_path = Path(__file__).with_name('check_ornith_cache.py')
    spec = importlib.util.spec_from_file_location('check_ornith_cache', checker_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load cache checker from {checker_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def missing_shard_names(report: dict[str, Any]) -> list[str]:
    return [Path(str(item['path'])).name for item in report.get('shards', []) if not item.get('complete')]


def _configure_hf_environment(hf_home: Path) -> None:
    os.environ['HF_HOME'] = str(hf_home)
    os.environ.setdefault('HF_HUB_CACHE', str(hf_home / 'hub'))
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')


def _load_snapshot_downloader() -> Downloader:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError('huggingface_hub is required when --allow-download is used') from exc
    return snapshot_download


def prepare_cache(
    *,
    hf_home: Path = DEFAULT_HF_HOME,
    repo_id: str = DEFAULT_REPO_ID,
    revision: str | None = None,
    allow_download: bool = False,
    downloader: Downloader | None = None,
) -> dict[str, Any]:
    hf_home = hf_home.resolve()
    _configure_hf_environment(hf_home)
    cache_checker = _load_cache_checker()
    before = cache_checker.inspect_cache(hf_home, repo_id, revision)
    resolved_revision = str(before.get('revision') or revision or '')
    missing = missing_shard_names(before)
    plan: dict[str, Any] = {
        'hf_home': str(hf_home),
        'repo_id': repo_id,
        'revision': resolved_revision or None,
        'download_permitted': allow_download,
        'download_attempted': False,
        'complete_before': bool(before.get('complete')),
        'complete_after': bool(before.get('complete')),
        'missing_shards': missing,
        'allow_patterns': missing,
        'cache_dir': str(hf_home / 'hub'),
    }
    if not allow_download or not missing:
        return plan

    active_downloader = downloader or _load_snapshot_downloader()
    active_downloader(
        repo_id=repo_id,
        revision=resolved_revision or None,
        cache_dir=str(hf_home / 'hub'),
        allow_patterns=missing,
    )
    after = cache_checker.inspect_cache(hf_home, repo_id, revision)
    plan['download_attempted'] = True
    plan['complete_after'] = bool(after.get('complete'))
    plan['missing_shards_after'] = missing_shard_names(after)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description='Prepare the local Ornith Hugging Face cache for vLLM.')
    parser.add_argument('--hf-home', type=Path, default=DEFAULT_HF_HOME)
    parser.add_argument('--repo-id', default=DEFAULT_REPO_ID)
    parser.add_argument('--revision')
    parser.add_argument('--allow-download', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()

    plan = prepare_cache(
        hf_home=args.hf_home,
        repo_id=args.repo_id,
        revision=args.revision,
        allow_download=args.allow_download,
    )
    encoded = json.dumps(plan, indent=2, sort_keys=True) + '\n'
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    if args.json or args.output is None:
        print(encoded, end='')
    if args.require_complete and not plan['complete_after']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
