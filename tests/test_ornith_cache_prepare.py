from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'prepare_ornith_cache.py'
REPO_ID = 'deepreinforce-ai/Ornith-1.0-9B'


def _load_module():
    spec = importlib.util.spec_from_file_location('prepare_ornith_cache', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_partial_cache(hf_home: Path) -> Path:
    repo_dir = hf_home / 'hub' / 'models--deepreinforce-ai--Ornith-1.0-9B'
    revision = 'abc123'
    snapshot_dir = repo_dir / 'snapshots' / revision
    (repo_dir / 'refs').mkdir(parents=True)
    snapshot_dir.mkdir(parents=True)
    (repo_dir / 'refs' / 'main').write_text(revision, encoding='utf-8')
    (snapshot_dir / 'model.safetensors.index.json').write_text(
        json.dumps(
            {
                'weight_map': {
                    'layer.1': 'model-00001-of-00004.safetensors',
                    'layer.2': 'model-00002-of-00004.safetensors',
                    'layer.3': 'model-00003-of-00004.safetensors',
                    'layer.4': 'model-00004-of-00004.safetensors',
                }
            }
        ),
        encoding='utf-8',
    )
    (snapshot_dir / 'model-00001-of-00004.safetensors').write_bytes(b'weights')
    return snapshot_dir


def test_dry_run_reports_missing_shards_without_downloading(tmp_path) -> None:
    module = _load_module()
    hf_home = tmp_path / '.cache' / 'huggingface'
    _write_partial_cache(hf_home)
    calls = []

    plan = module.prepare_cache(hf_home=hf_home, repo_id=REPO_ID, allow_download=False, downloader=calls.append)

    assert calls == []
    assert plan['download_permitted'] is False
    assert plan['download_attempted'] is False
    assert plan['complete_before'] is False
    assert plan['complete_after'] is False
    assert plan['missing_shards'] == [
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ]
    assert plan['allow_patterns'] == [
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ]


def test_allow_download_invokes_downloader_for_missing_shards_only(tmp_path) -> None:
    module = _load_module()
    hf_home = tmp_path / '.cache' / 'huggingface'
    snapshot_dir = _write_partial_cache(hf_home)
    calls = []

    def fake_downloader(**kwargs):
        calls.append(kwargs)
        for name in kwargs['allow_patterns']:
            (snapshot_dir / name).write_bytes(b'weights')
        return str(snapshot_dir)

    plan = module.prepare_cache(hf_home=hf_home, repo_id=REPO_ID, allow_download=True, downloader=fake_downloader)

    assert len(calls) == 1
    call = calls[0]
    assert call['repo_id'] == REPO_ID
    assert call['revision'] == 'abc123'
    assert call['cache_dir'] == str(hf_home / 'hub')
    assert call['allow_patterns'] == [
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ]
    assert plan['download_permitted'] is True
    assert plan['download_attempted'] is True
    assert plan['complete_before'] is False
    assert plan['complete_after'] is True


def test_allow_download_does_not_redownload_when_cache_is_complete(tmp_path) -> None:
    module = _load_module()
    hf_home = tmp_path / '.cache' / 'huggingface'
    snapshot_dir = _write_partial_cache(hf_home)
    for name in (
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ):
        (snapshot_dir / name).write_bytes(b'weights')
    calls = []

    plan = module.prepare_cache(hf_home=hf_home, repo_id=REPO_ID, allow_download=True, downloader=calls.append)

    assert calls == []
    assert plan['download_permitted'] is True
    assert plan['download_attempted'] is False
    assert plan['complete_before'] is True
    assert plan['complete_after'] is True
    assert plan['missing_shards'] == []
    assert plan['allow_patterns'] == []


def test_sdist_includes_prepare_cache_script() -> None:
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')

    assert 'include scripts/prepare_ornith_cache.py' in manifest
