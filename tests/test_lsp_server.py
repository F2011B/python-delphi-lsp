import inspect
from functools import partial
from types import SimpleNamespace
from unittest import mock

from lsprotocol.types import (
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_RENAME,
    Position,
    TextDocumentSyncKind,
)

from delphi_lsp.lsp_server import (
    WorkspaceConfig,
    build_outline_semantic_model,
    create_server,
    iter_symbols,
    text_references_for_symbol,
)
from delphi_lsp.semantic import SymbolKind


def test_server_advertises_full_document_synchronization() -> None:
    server = create_server()

    assert server._text_document_sync_kind == TextDocumentSyncKind.Full


def test_did_change_indexes_the_document_synchronized_by_pygls() -> None:
    server = create_server()
    uri = 'file:///ChangedUnit.pas'
    full_source = (
        'unit ChangedUnit;\n'
        'interface\n'
        'procedure Updated;\n'
        'implementation\n'
        'procedure Updated;\n'
        'begin\n'
        'end;\n'
        'end.\n'
    )
    server.lsp._workspace = SimpleNamespace(
        get_text_document=lambda document_uri: SimpleNamespace(source=full_source),
    )
    handler = server.lsp.fm.features[TEXT_DOCUMENT_DID_CHANGE]
    assert isinstance(handler, partial)
    state = inspect.getclosurevars(handler.func).nonlocals['state']
    params = SimpleNamespace(
        text_document=SimpleNamespace(uri=uri),
        content_changes=[
            SimpleNamespace(
                text='Updated',
                range=SimpleNamespace(),
            )
        ],
    )

    with mock.patch.object(server, 'publish_diagnostics'):
        handler(params)

    assert state.documents[uri].text == full_source


def test_text_references_scan_the_whole_unit_for_a_global_symbol(tmp_path) -> None:
    source = (
        'unit GlobalUnit;\n'
        'interface\n'
        'procedure UseFoo;\n'
        'implementation\n'
        'procedure UseFoo;\n'
        'begin\n'
        '  UseFoo;\n'
        'end;\n'
        'end.\n'
    )
    file_name = str(tmp_path / 'GlobalUnit.pas')
    model = build_outline_semantic_model(source, file_name)
    symbol = next(
        item
        for item in iter_symbols(model.unit_scope)
        if item.kind == SymbolKind.PROCEDURE
    )

    references = text_references_for_symbol(
        source,
        symbol,
        file_name=file_name,
        include_declaration=True,
    )

    assert [item.start_line for item in references] == [3, 5, 7]


def test_text_references_are_attributed_to_the_file_being_scanned(tmp_path) -> None:
    declaring_file = str(tmp_path / 'Worker.pas')
    current_file = str(tmp_path / 'Main.pas')
    model = build_outline_semantic_model(
        'unit Worker;\ninterface\nimplementation\nend.\n',
        declaring_file,
    )
    unit_symbol = next(
        item
        for item in iter_symbols(model.unit_scope)
        if item.kind == SymbolKind.UNIT
    )
    current_source = (
        'program Main;\n'
        'uses Worker;\n'
        'begin\n'
        '  Worker.Run;\n'
        'end.\n'
    )

    references = text_references_for_symbol(
        current_source,
        unit_symbol,
        file_name=current_file,
        include_declaration=True,
    )

    assert len(references) == 2
    assert {item.file_name for item in references} == {current_file}


def test_rename_aborts_when_an_include_makes_ranges_unsafe(tmp_path) -> None:
    include_path = tmp_path / 'Shared.inc'
    include_path.write_text('const Included = 1;\n', encoding='utf-8')
    source_path = tmp_path / 'IncludeRename.pas'
    source = (
        'unit IncludeRename;\n'
        'interface\n'
        '{$I Shared.inc}\n'
        'type\n'
        '  TLocal = Integer;\n'
        'var\n'
        '  Value: TLocal;\n'
        'implementation\n'
        'end.\n'
    )
    uri = source_path.as_uri()
    server = create_server()
    handler = server.lsp.fm.features[TEXT_DOCUMENT_RENAME]
    assert isinstance(handler, partial)
    state = inspect.getclosurevars(handler.func).nonlocals['state']
    state.configure(WorkspaceConfig(include_paths=[str(tmp_path)]))
    state.update_document(uri, source)
    params = SimpleNamespace(
        text_document=SimpleNamespace(uri=uri),
        position=Position(line=6, character=11),
        new_name='TRenamed',
    )

    result = handler(params)

    assert result is None
