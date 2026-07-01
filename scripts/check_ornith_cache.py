#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_HF_HOME = Path('/Volumes/MacDataSSDPro/.cache/huggingface')
DEFAULT_REPO_ID = 'deepreinforce-ai/Ornith-1.0-9B'


def repo_cache_dir(hf_home: Path, repo_id: str) -> Path:
    return hf_home / 'hub' / f"models--{repo_id.replace('/', '--')}"


def current_revision(repo_dir: Path) -> str | None:
    ref = repo_dir / 'refs' / 'main'
    if not ref.exists():
        return None
    return ref.read_text(encoding='utf-8').strip() or None


def shard_names(snapshot_dir: Path) -> list[str]:
    index_path = snapshot_dir / 'model.safetensors.index.json'
    if not index_path.exists():
        return []
    data = json.loads(index_path.read_text(encoding='utf-8'))
    return sorted(set(data.get('weight_map', {}).values()))


def file_status(path: Path) -> dict[str, object]:
    exists = path.exists()
    resolved = path.resolve() if exists else path
    complete = exists and resolved.is_file() and not resolved.name.endswith('.incomplete')
    return {
        'path': str(path),
        'resolved': str(resolved),
        'exists': exists,
        'size': resolved.stat().st_size if complete else 0,
        'complete': complete,
    }


def inspect_cache(hf_home: Path, repo_id: str, revision: str | None) -> dict[str, object]:
    repo_dir = repo_cache_dir(hf_home, repo_id)
    resolved_revision = revision or current_revision(repo_dir)
    snapshot_dir = repo_dir / 'snapshots' / resolved_revision if resolved_revision else None
    shards = shard_names(snapshot_dir) if snapshot_dir is not None and snapshot_dir.exists() else []
    shard_statuses = [
        file_status(snapshot_dir / shard) for shard in shards
    ] if snapshot_dir is not None else []
    incomplete_files = sorted(repo_dir.glob('blobs/*.incomplete')) if repo_dir.exists() else []
    missing = [item for item in shard_statuses if not item['complete']]
    complete = bool(shard_statuses) and not missing
    return {
        'hf_home': str(hf_home),
        'repo_id': repo_id,
        'repo_dir': str(repo_dir),
        'revision': resolved_revision,
        'snapshot_dir': str(snapshot_dir) if snapshot_dir is not None else None,
        'required_shards': shards,
        'shards': shard_statuses,
        'incomplete_files': [
            {'path': str(path), 'size': path.stat().st_size}
            for path in incomplete_files
        ],
        'complete': complete,
    }


def human_size(size: int) -> str:
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{size} B'


def print_text(report: dict[str, object]) -> None:
    print(f"HF_HOME: {report['hf_home']}")
    print(f"Repository: {report['repo_id']}")
    print(f"Revision: {report['revision'] or 'missing'}")
    print(f"Snapshot: {report['snapshot_dir'] or 'missing'}")
    print(f"Complete: {report['complete']}")
    print('Required shards:')
    for item in report['shards']:
        status = 'ok' if item['complete'] else 'missing'
        print(f"  {status:7} {human_size(int(item['size'])):>10} {item['path']}")
    incomplete = report['incomplete_files']
    if incomplete:
        print('Incomplete blob files:')
        for item in incomplete:
            print(f"  {human_size(int(item['size'])):>10} {item['path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description='Check whether Ornith HF cache can be used without downloading.')
    parser.add_argument('--hf-home', type=Path, default=DEFAULT_HF_HOME)
    parser.add_argument('--repo-id', default=DEFAULT_REPO_ID)
    parser.add_argument('--revision')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()

    report = inspect_cache(args.hf_home, args.repo_id, args.revision)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    if args.require_complete and not report['complete']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
