#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from delphiast.parser import parse  # noqa: E402
from delphiast.source_reader import read_source_text  # noqa: E402


SOURCE_SUFFIXES = {'.pas', '.dpr', '.dpk', '.inc'}


@dataclass
class FileResult:
    path: str
    repo: str
    suffix: str
    bytes: int
    lines: int
    large: bool
    status: str
    elapsed_ms: int
    semantic_problems: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    error_line: int | None = None
    error_column: int | None = None


def _git_head(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ['git', '-C', str(path), 'rev-parse', '--short', 'HEAD'],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _discover_roots(raw_roots: Iterable[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in raw_roots:
        path = (REPO_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if path.exists():
            roots.append(path)
    return roots


def _iter_sources(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix.lower() in SOURCE_SUFFIXES:
            files.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob('*'):
            if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
                files.append(path)
    return sorted(set(files), key=lambda p: str(p).casefold())


def _repo_name(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if root.name == 'github_repos' and rel.parts:
            return rel.parts[0]
        return root.name
    return path.parents[0].name


def _error_position(exc: Exception) -> tuple[int | None, int | None]:
    line = getattr(exc, 'line', None)
    column = getattr(exc, 'column', None)
    if column is None:
        column = getattr(exc, 'col', None)
    token = getattr(exc, 'token', None)
    if token is not None:
        line = line or getattr(token, 'line', None)
        column = column or getattr(token, 'column', None)
    return line, column


def _check_file(path: Path, roots: list[Path], large_threshold: int, build_semantic: bool) -> FileResult:
    start = perf_counter()
    data = path.read_bytes()
    text = read_source_text(path)
    lines = len(text.splitlines())
    try:
        result = parse(text, str(path), build_semantic=build_semantic)
        semantic_problems = len(result.semantic.problems) if result.semantic is not None else None
        return FileResult(
            path=str(path.relative_to(REPO_ROOT)),
            repo=_repo_name(path, roots),
            suffix=path.suffix.lower(),
            bytes=len(data),
            lines=lines,
            large=lines >= large_threshold,
            status='ok',
            elapsed_ms=round((perf_counter() - start) * 1000),
            semantic_problems=semantic_problems,
        )
    except Exception as exc:  # noqa: BLE001 - corpus runner records parser failures.
        line, column = _error_position(exc)
        return FileResult(
            path=str(path.relative_to(REPO_ROOT)),
            repo=_repo_name(path, roots),
            suffix=path.suffix.lower(),
            bytes=len(data),
            lines=lines,
            large=lines >= large_threshold,
            status='fail',
            elapsed_ms=round((perf_counter() - start) * 1000),
            error_type=exc.__class__.__name__,
            error_message=str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__,
            error_line=line,
            error_column=column,
        )


def _summarize(results: list[FileResult], roots: list[Path], large_threshold: int, semantic: bool) -> dict:
    by_repo: dict[str, Counter] = defaultdict(Counter)
    by_suffix: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        by_repo[item.repo][item.status] += 1
        by_repo[item.repo]['large'] += int(item.large)
        by_suffix[item.suffix][item.status] += 1
    commits = {
        root.name: _git_head(root)
        for root in roots
        if (root / '.git').exists()
    }
    github_root = next((root for root in roots if root.name == 'github_repos'), None)
    if github_root is not None:
        commits.update({
            child.name: _git_head(child)
            for child in sorted(github_root.iterdir())
            if child.is_dir() and (child / '.git').exists()
        })
    ok = sum(1 for item in results if item.status == 'ok')
    fail = len(results) - ok
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'roots': [str(root.relative_to(REPO_ROOT)) if root.is_relative_to(REPO_ROOT) else str(root) for root in roots],
        'repo_commits': commits,
        'semantic': semantic,
        'large_threshold_lines': large_threshold,
        'total_files': len(results),
        'ok': ok,
        'fail': fail,
        'large_files': sum(1 for item in results if item.large),
        'by_repo': {key: dict(value) for key, value in sorted(by_repo.items())},
        'by_suffix': {key: dict(value) for key, value in sorted(by_suffix.items())},
    }


def _write_markdown(path: Path, summary: dict, results: list[FileResult]) -> None:
    failures = [item for item in results if item.status == 'fail']
    largest = sorted(results, key=lambda item: item.lines, reverse=True)[:20]
    lines = [
        '# Delphi Corpus Check',
        '',
        f"- Generated: `{summary['generated_at']}`",
        f"- Semantic model: `{summary['semantic']}`",
        f"- Files: `{summary['total_files']}` ok `{summary['ok']}` fail `{summary['fail']}`",
        f"- Large files >= {summary['large_threshold_lines']} lines: `{summary['large_files']}`",
        '',
        '## Repositories',
        '',
        '| Repo | Commit | OK | Fail | Large |',
        '| --- | --- | ---: | ---: | ---: |',
    ]
    for repo, counts in summary['by_repo'].items():
        commit = summary['repo_commits'].get(repo) or ''
        lines.append(f"| {repo} | `{commit}` | {counts.get('ok', 0)} | {counts.get('fail', 0)} | {counts.get('large', 0)} |")
    lines.extend([
        '',
        '## Largest Files',
        '',
        '| Lines | Status | Path |',
        '| ---: | --- | --- |',
    ])
    for item in largest:
        lines.append(f"| {item.lines} | {item.status} | `{item.path}` |")
    lines.extend([
        '',
        '## Failures',
        '',
    ])
    if not failures:
        lines.append('No parser failures.')
    else:
        lines.extend([
            '| Repo | Line | Column | Error | Path |',
            '| --- | ---: | ---: | --- | --- |',
        ])
        for item in failures:
            message = (item.error_message or '').replace('|', '\\|')
            lines.append(
                f"| {item.repo} | {item.error_line or ''} | {item.error_column or ''} | "
                f"{item.error_type}: {message} | `{item.path}` |"
            )
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Run Delphi parser/LSP corpus checks over local source trees.')
    parser.add_argument(
        'roots',
        nargs='*',
        default=['test_projects/github_repos'],
        help='Files or directories to scan. Defaults to test_projects/github_repos.',
    )
    parser.add_argument('--output-dir', default='output/corpus', help='Directory for report artifacts.')
    parser.add_argument('--large-threshold-lines', type=int, default=5000)
    parser.add_argument('--limit', type=int, default=0, help='Limit number of files after stable path sorting.')
    parser.add_argument('--no-semantic', action='store_true', help='Skip semantic model construction.')
    parser.add_argument('--fail-on-error', action='store_true', help='Return a non-zero exit code if any file fails.')
    args = parser.parse_args()

    roots = _discover_roots(args.roots)
    if not roots:
        print('No existing roots to scan.', file=sys.stderr)
        return 2
    files = _iter_sources(roots)
    if args.limit > 0:
        files = files[: args.limit]

    output_dir = (REPO_ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    build_semantic = not args.no_semantic
    results = [
        _check_file(path, roots, args.large_threshold_lines, build_semantic)
        for path in files
    ]
    summary = _summarize(results, roots, args.large_threshold_lines, build_semantic)
    payload = {
        'summary': summary,
        'results': [asdict(result) for result in results],
    }
    json_path = output_dir / 'corpus_report.json'
    md_path = output_dir / 'corpus_report.md'
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _write_markdown(md_path, summary, results)

    print(f"wrote {json_path.relative_to(REPO_ROOT)}")
    print(f"wrote {md_path.relative_to(REPO_ROOT)}")
    print(f"files={summary['total_files']} ok={summary['ok']} fail={summary['fail']} large={summary['large_files']}")
    return 1 if args.fail_on_error and summary['fail'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
