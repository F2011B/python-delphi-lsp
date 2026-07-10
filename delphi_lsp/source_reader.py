from __future__ import annotations

from pathlib import Path


def read_source_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        text = data.decode('utf-16')
    elif data.startswith(b'\xef\xbb\xbf'):
        text = data.decode('utf-8-sig')
    else:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            text = data.decode('latin-1')
    return text.replace('\r\n', '\n').replace('\r', '\n')
