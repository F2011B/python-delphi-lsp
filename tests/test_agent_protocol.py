import base64
import importlib
import json
import unicodedata

import pytest


def _protocol():
    return importlib.import_module('delphi_lsp.agent_protocol')


def _encode_raw_cursor(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')


def _compact_json_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


class OneShotItems:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.iterations = 0

    def __iter__(self):
        if self.iterations:
            raise AssertionError('One-shot iterable was consumed more than once.')
        self.iterations += 1
        yield from self.items


def test_schema_and_supported_values_are_versioned_and_deterministic() -> None:
    protocol = _protocol()

    assert protocol.SCHEMA_VERSION == 2
    assert type(protocol.SCHEMA_VERSION) is int
    assert protocol.SUPPORTED_ACTIONS == (
        'open',
        'find',
        'inspect',
        'trace',
        'focus',
        'problems',
        'metrics',
    )


def test_request_accepts_additive_metrics_action() -> None:
    protocol = _protocol()

    request = protocol.AgentRequest.from_mapping({'action': 'metrics', 'query': 'Alpha'})

    assert request.action == 'metrics'
    assert request.query == 'Alpha'
    assert protocol.SUPPORTED_DETAILS == (
        'summary',
        'declaration',
        'members',
        'context',
        'body',
        'implementations',
    )
    assert protocol.SUPPORTED_RELATIONS == (
        'references',
        'callers',
        'callees',
        'uses',
        'used_by',
        'inherits',
        'implements',
    )


def test_request_defaults_and_serializes_all_protocol_fields() -> None:
    protocol = _protocol()

    request = protocol.AgentRequest.from_mapping({'action': 'open'})

    assert request.action == 'open'
    assert request.query == ''
    assert request.target_id == ''
    assert request.project_id == ''
    assert request.detail == 'summary'
    assert request.relation is None
    assert request.cursor == ''
    assert request.max_items == 12
    assert request.max_chars == 12000
    assert request.to_mapping() == {
        'action': 'open',
        'query': '',
        'target_id': '',
        'project_id': '',
        'detail': 'summary',
        'relation': None,
        'cursor': '',
        'max_items': 12,
        'max_chars': 12000,
    }


def test_request_accepts_every_declared_field() -> None:
    protocol = _protocol()
    mapping = {
        'action': 'trace',
        'query': 'Run',
        'target_id': 'target-1',
        'project_id': 'project-1',
        'detail': 'body',
        'relation': 'callers',
        'cursor': 'cursor-1',
        'max_items': 50,
        'max_chars': 40000,
    }

    assert protocol.AgentRequest.from_mapping(mapping).to_mapping() == mapping


def test_request_rejects_unknown_fields_with_stable_error() -> None:
    protocol = _protocol()

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping({'action': 'open', 'extra': True})

    assert caught.value.code == 'unknown_field'
    assert caught.value.message == 'Unknown request field: extra.'
    assert str(caught.value) == caught.value.message


def test_request_requires_action_with_stable_error() -> None:
    protocol = _protocol()

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping({})

    assert caught.value.code == 'missing_field'
    assert caught.value.message == 'Missing required request field: action.'


@pytest.mark.parametrize(
    ('field', 'value', 'code', 'message'),
    [
        ('action', 'jump', 'invalid_action', "Unsupported action value: 'jump'."),
        ('detail', 'full', 'invalid_detail', "Unsupported detail value: 'full'."),
        ('relation', 'parents', 'invalid_relation', "Unsupported relation value: 'parents'."),
    ],
)
def test_request_rejects_invalid_enum_values(
    field: str,
    value: str,
    code: str,
    message: str,
) -> None:
    protocol = _protocol()
    mapping = {'action': 'inspect', field: value}

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping(mapping)

    assert caught.value.code == code
    assert caught.value.message == message


@pytest.mark.parametrize('field', ['action', 'query', 'target_id', 'project_id', 'detail', 'cursor'])
def test_request_rejects_non_string_string_fields(field: str) -> None:
    protocol = _protocol()
    mapping = {'action': 'open', field: None}

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping(mapping)

    assert caught.value.code == 'invalid_type'
    assert caught.value.message == f"Field '{field}' must be a string."


def test_request_accepts_an_explicit_absent_relation() -> None:
    protocol = _protocol()

    request = protocol.AgentRequest.from_mapping({'action': 'find', 'relation': None})

    assert request.relation is None


@pytest.mark.parametrize('field', ['max_items', 'max_chars'])
def test_request_rejects_booleans_as_integers(field: str) -> None:
    protocol = _protocol()
    mapping = {'action': 'find', field: True}

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping(mapping)

    assert caught.value.code == 'invalid_type'
    assert caught.value.message == f"Field '{field}' must be an integer."


@pytest.mark.parametrize(
    ('field', 'value', 'code', 'message'),
    [
        ('max_items', 0, 'max_items_out_of_range', "Field 'max_items' must be between 1 and 50."),
        ('max_items', 51, 'max_items_out_of_range', "Field 'max_items' must be between 1 and 50."),
        ('max_chars', 255, 'max_chars_out_of_range', "Field 'max_chars' must be between 256 and 40000."),
        ('max_chars', 40001, 'max_chars_out_of_range', "Field 'max_chars' must be between 256 and 40000."),
    ],
)
def test_request_rejects_out_of_range_limits(
    field: str,
    value: int,
    code: str,
    message: str,
) -> None:
    protocol = _protocol()
    mapping = {'action': 'find', field: value}

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.AgentRequest.from_mapping(mapping)

    assert caught.value.code == code
    assert caught.value.message == message


@pytest.mark.parametrize(
    ('chars', 'approx_tokens'),
    [(0, 0), (1, 1), (4, 1), (5, 2), (12000, 3000)],
)
def test_context_budget_uses_deterministic_ceiling(chars: int, approx_tokens: int) -> None:
    protocol = _protocol()

    context = protocol.ContextBudget(chars=chars)

    assert context.approx_tokens == approx_tokens
    assert context.to_mapping() == {'chars': chars, 'approx_tokens': approx_tokens}


def test_focus_and_page_are_serializable_value_objects() -> None:
    protocol = _protocol()

    focus = protocol.Focus(project_id='project-1', unit_id='unit-1', target_id='target-1')
    page = protocol.Page(returned=2, total=5, truncated=True, next_cursor='next-1')

    assert focus.to_mapping() == {
        'project_id': 'project-1',
        'unit_id': 'unit-1',
        'target_id': 'target-1',
    }
    assert page.to_mapping() == {
        'returned': 2,
        'total': 5,
        'truncated': True,
        'next_cursor': 'next-1',
    }


def test_focus_carries_project_unit_and_target_identifiers() -> None:
    protocol = _protocol()

    focus = protocol.Focus(
        project_id='project-1',
        unit_id='unit-1',
        target_id='target-1',
    )

    assert focus.project_id == 'project-1'
    assert focus.unit_id == 'unit-1'
    assert focus.target_id == 'target-1'
    assert focus.to_mapping() == {
        'project_id': 'project-1',
        'unit_id': 'unit-1',
        'target_id': 'target-1',
    }


def test_success_response_contains_the_complete_versioned_envelope() -> None:
    protocol = _protocol()
    response = protocol.AgentResponse(
        workspace_revision='revision-7',
        focus=protocol.Focus(project_id='project-1', unit_id='unit-1', target_id='target-1'),
        result={'items': [{'id': 'target-1'}]},
        page=protocol.Page(returned=1, total=1, truncated=False, next_cursor=''),
        context=protocol.ContextBudget(chars=17),
    )

    mapping = response.to_mapping()

    assert list(mapping) == ['schema', 'workspace_revision', 'focus', 'result', 'page', 'context']
    assert mapping == {
        'schema': 2,
        'workspace_revision': 'revision-7',
        'focus': {'project_id': 'project-1', 'unit_id': 'unit-1', 'target_id': 'target-1'},
        'result': {'items': [{'id': 'target-1'}]},
        'page': {'returned': 1, 'total': 1, 'truncated': False, 'next_cursor': ''},
        'context': {'chars': 17, 'approx_tokens': 5},
    }
    assert json.loads(json.dumps(mapping, ensure_ascii=False)) == mapping


def test_target_id_normalizes_case_and_relative_path_separators() -> None:
    protocol = _protocol()

    windows_style = protocol.make_target_id('Method', r'.\Src\Domain\Order.PAS', 'TOrder.Save')
    posix_style = protocol.make_target_id('method', 'src/domain/order.pas', 'torder.save')

    assert windows_style == posix_style
    assert windows_style.startswith('target_v2_')
    assert all(character.isascii() and (character.isalnum() or character == '_') for character in windows_style)


def test_target_id_normalizes_canonically_equivalent_unicode() -> None:
    protocol = _protocol()
    composed = ('Méthod', 'Src/Café/Unité.pas', 'TÉlément.Exécuter')
    decomposed = tuple(unicodedata.normalize('NFD', value) for value in composed)

    assert protocol.make_target_id(*composed) == protocol.make_target_id(*decomposed)


def test_target_id_is_deterministic_and_contains_no_workspace_or_line_data() -> None:
    protocol = _protocol()

    first = protocol.make_target_id('class', 'src/model.pas', 'TModel')
    second = protocol.make_target_id('class', 'src/model.pas', 'TModel')

    assert first == second
    assert 'src' not in first
    assert 'model' not in first
    assert '/Volumes/' not in first


def test_target_id_distinguishes_every_identity_component() -> None:
    protocol = _protocol()
    identifiers = {
        protocol.make_target_id('class', 'src/model.pas', 'TModel', 0),
        protocol.make_target_id('record', 'src/model.pas', 'TModel', 0),
        protocol.make_target_id('class', 'src/other.pas', 'TModel', 0),
        protocol.make_target_id('class', 'src/model.pas', 'TOther', 0),
        protocol.make_target_id('class', 'src/model.pas', 'TModel', 1),
    }

    assert len(identifiers) == 5


def test_cursor_round_trip_is_deterministic_and_url_safe() -> None:
    protocol = _protocol()

    cursor = protocol.encode_cursor('revision-2', 17, 'find:order')

    assert cursor == protocol.encode_cursor('revision-2', 17, 'find:order')
    assert cursor
    assert '=' not in cursor
    assert all(character.isascii() and (character.isalnum() or character in '-_') for character in cursor)
    assert protocol.decode_cursor(cursor, 'revision-2', 'find:order') == 17


@pytest.mark.parametrize('cursor', ['', 'not*url-safe', _encode_raw_cursor(['not', 'a', 'mapping'])])
def test_cursor_rejects_malformed_data(cursor: str) -> None:
    protocol = _protocol()

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.decode_cursor(cursor, 'revision-2', 'find:order')

    assert caught.value.code == 'malformed_cursor'
    assert caught.value.message == 'Cursor is malformed.'


@pytest.mark.parametrize('schema', [2.0, True])
def test_cursor_rejects_non_integer_schema_values(schema: object) -> None:
    protocol = _protocol()
    cursor = _encode_raw_cursor(
        {
            'fingerprint': 'find:item',
            'offset': 0,
            'revision': 'revision-2',
            'schema': schema,
        }
    )

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.decode_cursor(cursor, 'revision-2', 'find:item')

    assert caught.value.code == 'malformed_cursor'
    assert caught.value.message == 'Cursor is malformed.'


def test_cursor_rejects_a_stale_workspace_revision() -> None:
    protocol = _protocol()
    cursor = protocol.encode_cursor('revision-1', 3, 'inspect:item')

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.decode_cursor(cursor, 'revision-2', 'inspect:item')

    assert caught.value.code == 'stale_cursor'
    assert caught.value.message == 'Cursor workspace revision is stale.'


def test_cursor_rejects_a_mismatched_fingerprint() -> None:
    protocol = _protocol()
    cursor = protocol.encode_cursor('revision-2', 3, 'inspect:item')

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.decode_cursor(cursor, 'revision-2', 'trace:item')

    assert caught.value.code == 'cursor_mismatch'
    assert caught.value.message == 'Cursor fingerprint does not match.'


def test_cursor_encoder_rejects_a_negative_offset() -> None:
    protocol = _protocol()

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.encode_cursor('revision-2', -1, 'find:item')

    assert caught.value.code == 'negative_cursor'
    assert caught.value.message == 'Cursor offset cannot be negative.'


def test_cursor_decoder_rejects_a_negative_offset() -> None:
    protocol = _protocol()
    cursor = _encode_raw_cursor(
        {
            'fingerprint': 'find:item',
            'offset': -1,
            'revision': 'revision-2',
            'schema': 2,
        }
    )

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.decode_cursor(cursor, 'revision-2', 'find:item')

    assert caught.value.code == 'negative_cursor'
    assert caught.value.message == 'Cursor offset cannot be negative.'


@pytest.mark.parametrize(
    ('cursor_case', 'code'),
    [
        ('malformed', 'malformed_cursor'),
        ('stale', 'stale_cursor'),
        ('mismatched', 'cursor_mismatch'),
    ],
)
def test_paginate_items_rejects_cursor_before_iterating_one_shot_items(
    cursor_case: str,
    code: str,
) -> None:
    protocol = _protocol()
    if cursor_case == 'malformed':
        cursor = 'not*url-safe'
    elif cursor_case == 'stale':
        cursor = protocol.encode_cursor('revision-1', 0, 'find:one-shot')
    else:
        cursor = protocol.encode_cursor('revision-2', 0, 'find:other')
    items = OneShotItems([{'id': 1}])

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.paginate_items(
            items,
            revision='revision-2',
            fingerprint='find:one-shot',
            max_items=12,
            max_chars=256,
            cursor=cursor,
        )

    assert caught.value.code == code
    assert items.iterations == 0


def test_paginate_items_limits_count_and_continues_without_dropping_items() -> None:
    protocol = _protocol()
    items = [{'id': 1}, {'id': 2}, {'id': 3}]

    first_page, first_items = protocol.paginate_items(
        items,
        revision='revision-2',
        fingerprint='find:items',
        max_items=2,
        max_chars=1000,
    )

    assert first_items == items[:2]
    assert first_page.to_mapping() == {
        'returned': 2,
        'total': 3,
        'truncated': True,
        'next_cursor': first_page.next_cursor,
    }
    assert first_page.next_cursor
    assert protocol.decode_cursor(first_page.next_cursor, 'revision-2', 'find:items') == 2

    second_page, second_items = protocol.paginate_items(
        items,
        revision='revision-2',
        fingerprint='find:items',
        max_items=2,
        max_chars=1000,
        cursor=first_page.next_cursor,
    )

    assert second_items == items[2:]
    assert second_page.to_mapping() == {
        'returned': 1,
        'total': 3,
        'truncated': False,
        'next_cursor': '',
    }
    assert first_items + second_items == items


def test_paginate_items_uses_compact_json_character_budget() -> None:
    protocol = _protocol()
    items = [
        {'id': 1, 'text': 'é' * 120},
        {'id': 2, 'text': 'x' * 120},
        {'id': 3, 'text': 'y' * 120},
    ]
    max_chars = _compact_json_chars(items[:2])

    page, selected = protocol.paginate_items(
        items,
        revision='revision-2',
        fingerprint='find:unicode',
        max_items=10,
        max_chars=max_chars,
    )

    assert selected == items[:2]
    assert _compact_json_chars(selected) == max_chars
    assert page.returned == 2
    assert page.total == 3
    assert page.truncated is True
    assert page.next_cursor

    next_page, remaining = protocol.paginate_items(
        items,
        revision='revision-2',
        fingerprint='find:unicode',
        max_items=10,
        max_chars=max_chars,
        cursor=page.next_cursor,
    )

    assert remaining == items[2:]
    assert next_page.to_mapping() == {
        'returned': 1,
        'total': 3,
        'truncated': False,
        'next_cursor': '',
    }
    assert selected + remaining == items


def test_paginate_items_returns_an_untruncated_empty_page() -> None:
    protocol = _protocol()

    page, selected = protocol.paginate_items(
        [],
        revision='revision-2',
        fingerprint='problems',
        max_items=12,
        max_chars=256,
    )

    assert selected == []
    assert page.to_mapping() == {
        'returned': 0,
        'total': 0,
        'truncated': False,
        'next_cursor': '',
    }


@pytest.mark.parametrize(
    ('field', 'value', 'code', 'message'),
    [
        ('max_items', True, 'invalid_type', "Field 'max_items' must be an integer."),
        ('max_items', '12', 'invalid_type', "Field 'max_items' must be an integer."),
        ('max_items', 0, 'max_items_out_of_range', "Field 'max_items' must be between 1 and 50."),
        ('max_items', 51, 'max_items_out_of_range', "Field 'max_items' must be between 1 and 50."),
        ('max_chars', True, 'invalid_type', "Field 'max_chars' must be an integer."),
        ('max_chars', '256', 'invalid_type', "Field 'max_chars' must be an integer."),
        ('max_chars', 255, 'max_chars_out_of_range', "Field 'max_chars' must be between 256 and 40000."),
        ('max_chars', 40001, 'max_chars_out_of_range', "Field 'max_chars' must be between 256 and 40000."),
    ],
)
def test_paginate_items_validates_limits_like_agent_request(
    field: str,
    value: object,
    code: str,
    message: str,
) -> None:
    protocol = _protocol()
    limits = {'max_items': 12, 'max_chars': 256}
    limits[field] = value

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.paginate_items(
            [{'id': 1}],
            revision='revision-2',
            fingerprint='find:limits',
            **limits,
        )

    assert caught.value.code == code
    assert caught.value.message == message


def test_paginate_items_rejects_an_individual_oversized_item() -> None:
    protocol = _protocol()
    items = [{'text': 'x' * 300}]

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.paginate_items(
            items,
            revision='revision-2',
            fingerprint='inspect:large',
            max_items=12,
            max_chars=256,
        )

    assert caught.value.code == 'item_too_large'
    assert caught.value.message == 'Serialized item exceeds max_chars.'


def test_paginate_items_rejects_an_oversized_item_after_selected_items() -> None:
    protocol = _protocol()
    items = [{'id': 1}, {'text': 'x' * 300}]

    with pytest.raises(protocol.AgentProtocolError) as caught:
        protocol.paginate_items(
            items,
            revision='revision-2',
            fingerprint='inspect:later-large',
            max_items=12,
            max_chars=256,
        )

    assert caught.value.code == 'item_too_large'
    assert caught.value.message == 'Serialized item exceeds max_chars.'


def test_paginate_items_is_deterministic_for_the_same_inputs() -> None:
    protocol = _protocol()
    items = [{'b': 2, 'a': 1}, {'name': 'second'}, {'name': 'third'}]
    arguments = {
        'revision': 'revision-2',
        'fingerprint': 'find:stable',
        'max_items': 2,
        'max_chars': 256,
    }

    first_page, first_items = protocol.paginate_items(items, **arguments)
    second_page, second_items = protocol.paginate_items(items, **arguments)

    assert first_page == second_page
    assert first_items == second_items


def test_public_exports_contain_only_the_protocol_contract() -> None:
    protocol = _protocol()

    assert protocol.__all__ == [
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
