from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import posixpath


SCHEMA_VERSION = 2

SUPPORTED_ACTIONS = (
    'open',
    'find',
    'inspect',
    'trace',
    'focus',
    'problems',
)

SUPPORTED_DETAILS = (
    'summary',
    'declaration',
    'members',
    'context',
    'body',
    'implementations',
)

SUPPORTED_RELATIONS = (
    'references',
    'callers',
    'callees',
    'uses',
    'used_by',
    'inherits',
    'implements',
)

_REQUEST_FIELDS = frozenset(
    {
        'action',
        'query',
        'target_id',
        'project_id',
        'detail',
        'relation',
        'cursor',
        'max_items',
        'max_chars',
    }
)


class AgentProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class AgentRequest:
    action: str
    query: str = ''
    target_id: str = ''
    project_id: str = ''
    detail: str = 'summary'
    relation: str | None = None
    cursor: str = ''
    max_items: int = 12
    max_chars: int = 12000

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> AgentRequest:
        if not isinstance(mapping, Mapping):
            raise AgentProtocolError('invalid_request', 'Request must be a mapping.')

        unknown_fields = sorted(set(mapping) - _REQUEST_FIELDS, key=str)
        if unknown_fields:
            names = ', '.join(str(field) for field in unknown_fields)
            noun = 'field' if len(unknown_fields) == 1 else 'fields'
            raise AgentProtocolError('unknown_field', f'Unknown request {noun}: {names}.')
        if 'action' not in mapping:
            raise AgentProtocolError('missing_field', 'Missing required request field: action.')

        values = {
            'action': mapping['action'],
            'query': mapping.get('query', ''),
            'target_id': mapping.get('target_id', ''),
            'project_id': mapping.get('project_id', ''),
            'detail': mapping.get('detail', 'summary'),
            'relation': mapping.get('relation'),
            'cursor': mapping.get('cursor', ''),
            'max_items': mapping.get('max_items', 12),
            'max_chars': mapping.get('max_chars', 12000),
        }

        for field_name in ('action', 'query', 'target_id', 'project_id', 'detail', 'cursor'):
            if not isinstance(values[field_name], str):
                raise AgentProtocolError('invalid_type', f"Field '{field_name}' must be a string.")

        relation = values['relation']
        if relation is not None and not isinstance(relation, str):
            raise AgentProtocolError('invalid_type', "Field 'relation' must be a string or null.")

        for field_name in ('max_items', 'max_chars'):
            value = values[field_name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise AgentProtocolError('invalid_type', f"Field '{field_name}' must be an integer.")

        action = values['action']
        detail = values['detail']
        if action not in SUPPORTED_ACTIONS:
            raise AgentProtocolError('invalid_action', f'Unsupported action value: {action!r}.')
        if detail not in SUPPORTED_DETAILS:
            raise AgentProtocolError('invalid_detail', f'Unsupported detail value: {detail!r}.')
        if relation is not None and relation not in SUPPORTED_RELATIONS:
            raise AgentProtocolError('invalid_relation', f'Unsupported relation value: {relation!r}.')

        max_items = values['max_items']
        max_chars = values['max_chars']
        if not 1 <= max_items <= 50:
            raise AgentProtocolError(
                'max_items_out_of_range',
                "Field 'max_items' must be between 1 and 50.",
            )
        if not 256 <= max_chars <= 40000:
            raise AgentProtocolError(
                'max_chars_out_of_range',
                "Field 'max_chars' must be between 256 and 40000.",
            )

        return cls(
            action=action,
            query=values['query'],
            target_id=values['target_id'],
            project_id=values['project_id'],
            detail=detail,
            relation=relation,
            cursor=values['cursor'],
            max_items=max_items,
            max_chars=max_chars,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            'action': self.action,
            'query': self.query,
            'target_id': self.target_id,
            'project_id': self.project_id,
            'detail': self.detail,
            'relation': self.relation,
            'cursor': self.cursor,
            'max_items': self.max_items,
            'max_chars': self.max_chars,
        }


@dataclass(frozen=True)
class Focus:
    project_id: str = ''
    target_id: str = ''

    def to_mapping(self) -> dict[str, str]:
        return {
            'project_id': self.project_id,
            'target_id': self.target_id,
        }


@dataclass(frozen=True)
class Page:
    returned: int = 0
    total: int = 0
    truncated: bool = False
    next_cursor: str = ''

    def to_mapping(self) -> dict[str, object]:
        return {
            'returned': self.returned,
            'total': self.total,
            'truncated': self.truncated,
            'next_cursor': self.next_cursor,
        }


@dataclass(frozen=True)
class ContextBudget:
    chars: int

    @property
    def approx_tokens(self) -> int:
        return (self.chars + 3) // 4

    def to_mapping(self) -> dict[str, int]:
        return {
            'chars': self.chars,
            'approx_tokens': self.approx_tokens,
        }


@dataclass(frozen=True)
class AgentResponse:
    workspace_revision: str
    focus: Focus
    result: object
    page: Page
    context: ContextBudget

    @property
    def schema(self) -> int:
        return SCHEMA_VERSION

    def to_mapping(self) -> dict[str, object]:
        return {
            'schema': self.schema,
            'workspace_revision': self.workspace_revision,
            'focus': self.focus.to_mapping(),
            'result': self.result,
            'page': self.page.to_mapping(),
            'context': self.context.to_mapping(),
        }


def make_target_id(
    kind: str,
    relative_path: str,
    qualified_name: str,
    ordinal: int = 0,
) -> str:
    normalized_path = posixpath.normpath(relative_path.replace('\\', '/'))
    if normalized_path == '.':
        normalized_path = ''
    identity = [
        kind.casefold(),
        normalized_path.casefold(),
        qualified_name.casefold(),
        ordinal,
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return f'target_v2_{hashlib.sha256(encoded).hexdigest()}'


def encode_cursor(revision: str, offset: int, fingerprint: str) -> str:
    if not isinstance(revision, str) or not isinstance(fingerprint, str):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')
    if offset < 0:
        raise AgentProtocolError('negative_cursor', 'Cursor offset cannot be negative.')

    payload = {
        'fingerprint': fingerprint,
        'offset': offset,
        'revision': revision,
        'schema': SCHEMA_VERSION,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')


def decode_cursor(cursor: str, expected_revision: str, expected_fingerprint: str) -> int:
    if (
        not isinstance(cursor, str)
        or not cursor
        or not cursor.isascii()
        or any(not (character.isalnum() or character in '-_') for character in cursor)
    ):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')

    try:
        padding = '=' * (-len(cursor) % 4)
        decoded = base64.b64decode(cursor + padding, altchars=b'-_', validate=True)
        payload = json.loads(decoded.decode('utf-8'))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.') from None

    expected_fields = {'fingerprint', 'offset', 'revision', 'schema'}
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')
    if payload['schema'] != SCHEMA_VERSION:
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')
    if not isinstance(payload['revision'], str) or not isinstance(payload['fingerprint'], str):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')

    offset = payload['offset']
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')
    if offset < 0:
        raise AgentProtocolError('negative_cursor', 'Cursor offset cannot be negative.')
    if payload['revision'] != expected_revision:
        raise AgentProtocolError('stale_cursor', 'Cursor workspace revision is stale.')
    if payload['fingerprint'] != expected_fingerprint:
        raise AgentProtocolError('cursor_mismatch', 'Cursor fingerprint does not match.')
    return offset


def paginate_items(
    items: Iterable[object],
    revision: str,
    fingerprint: str,
    max_items: int,
    max_chars: int,
    cursor: str = '',
) -> tuple[Page, list[object]]:
    all_items = list(items)
    offset = decode_cursor(cursor, revision, fingerprint) if cursor else 0
    if offset > len(all_items):
        raise AgentProtocolError('malformed_cursor', 'Cursor is malformed.')

    selected: list[object] = []
    selected_chars = 2  # Compact JSON brackets around the selected list.
    for item in all_items[offset:]:
        if len(selected) >= max_items:
            break
        try:
            serialized = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise AgentProtocolError('invalid_item', 'Item is not JSON-compatible.') from None

        item_chars = len(serialized)
        separator_chars = 1 if selected else 0
        if item_chars > max_chars:
            raise AgentProtocolError('item_too_large', 'Serialized item exceeds max_chars.')
        if selected_chars + separator_chars + item_chars > max_chars:
            if not selected:
                raise AgentProtocolError('item_too_large', 'Serialized item exceeds max_chars.')
            break
        selected.append(item)
        selected_chars += separator_chars + item_chars

    next_offset = offset + len(selected)
    truncated = next_offset < len(all_items)
    next_cursor = encode_cursor(revision, next_offset, fingerprint) if truncated else ''
    page = Page(
        returned=len(selected),
        total=len(all_items),
        truncated=truncated,
        next_cursor=next_cursor,
    )
    return page, selected


__all__ = [
    'SCHEMA_VERSION',
    'SUPPORTED_ACTIONS',
    'SUPPORTED_DETAILS',
    'SUPPORTED_RELATIONS',
    'AgentProtocolError',
    'AgentRequest',
    'Focus',
    'Page',
    'ContextBudget',
    'AgentResponse',
    'make_target_id',
    'encode_cursor',
    'decode_cursor',
    'paginate_items',
]
