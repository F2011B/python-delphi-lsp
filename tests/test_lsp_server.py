import inspect
from functools import partial
from types import SimpleNamespace
from unittest import mock

from lsprotocol.types import (
    INITIALIZE,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_CLOSE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_RENAME,
    Position,
    TextDocumentSyncKind,
)
from pygls.feature_manager import is_thread_function

from delphi_lsp import lsp_server
from delphi_lsp.lsp_server import (
    LspWorkspaceState,
    WorkspaceConfig,
    build_outline_semantic_model,
    create_server,
    iter_symbols,
    text_references_for_symbol,
)
from delphi_lsp.project_discovery import SKIP_DIRS
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


def test_document_updates_reparse_only_the_changed_file(tmp_path) -> None:
    first_path = tmp_path / 'First.pas'
    second_path = tmp_path / 'Second.pas'
    first_source = 'unit First;\ninterface\nimplementation\nend.\n'
    second_source = 'unit Second;\ninterface\nimplementation\nend.\n'
    first_path.write_text(first_source, encoding='utf-8')
    second_path.write_text(second_source, encoding='utf-8')
    state = LspWorkspaceState()
    state.configure(WorkspaceConfig(roots=[str(tmp_path)]))
    state.index_workspace()
    unchanged_model = state.workspace.models[str(second_path)]

    with mock.patch.object(
        lsp_server,
        'build_outline_semantic_model',
        wraps=lsp_server.build_outline_semantic_model,
    ) as build_outline:
        state.update_document(
            first_path.as_uri(),
            first_source.replace('interface', 'interface\nuses Second;'),
        )

    assert [call.args[1] for call in build_outline.call_args_list] == [
        str(first_path)
    ]
    assert state.workspace.models[str(second_path)] is unchanged_model

    with mock.patch.object(
        lsp_server,
        'build_outline_semantic_model',
        wraps=lsp_server.build_outline_semantic_model,
    ) as build_outline:
        state.remove_document(first_path.as_uri())

    assert build_outline.call_count == 0
    assert state.workspace.models[str(second_path)] is unchanged_model


def test_document_notifications_are_dispatched_off_the_event_loop() -> None:
    server = create_server()

    for method in (
        TEXT_DOCUMENT_DID_OPEN,
        TEXT_DOCUMENT_DID_CHANGE,
        TEXT_DOCUMENT_DID_CLOSE,
    ):
        handler = server.lsp.fm.features[method]
        assert isinstance(handler, partial)
        assert is_thread_function(handler)


def test_workspace_symbol_query_cache_is_a_bounded_lru() -> None:
    state = LspWorkspaceState()

    for query in 'abcdefghi':
        state.workspace_symbols_for_query(query)
    state.workspace_symbols_for_query('b')
    state.workspace_symbols_for_query('j')

    assert len(state.workspace_symbol_query_cache) == 8
    assert 'b' in state.workspace_symbol_query_cache
    assert 'c' not in state.workspace_symbol_query_cache


def test_lsp_workspace_scan_prunes_shared_skip_directories(tmp_path) -> None:
    source_dir = tmp_path / 'src'
    source_dir.mkdir()
    source_path = source_dir / 'Canonical.pas'
    source_path.write_text(
        'unit Canonical;\ninterface\nimplementation\nend.\n',
        encoding='utf-8',
    )
    for index, directory_name in enumerate(sorted(SKIP_DIRS)):
        skipped_dir = tmp_path / directory_name
        skipped_dir.mkdir()
        (skipped_dir / f'Copy{index}.pas').write_text(
            f'unit Copy{index};\ninterface\nimplementation\nend.\n',
            encoding='utf-8',
        )
    state = LspWorkspaceState()
    state.configure(WorkspaceConfig(roots=[str(tmp_path)]))

    files = state._scan_workspace_files()

    assert files == [str(source_path)]


def test_invalid_project_config_degrades_to_warning(tmp_path) -> None:
    (tmp_path / '.delphi-lsp.toml').write_text(
        '[projects]\nincludes = ["*.dpr"]\n',
        encoding='utf-8',
    )
    state = LspWorkspaceState()

    state.configure(WorkspaceConfig(roots=[str(tmp_path)]))

    assert state.config.roots == [str(tmp_path)]
    assert state.project_configs == ()
    assert len(state.config_warnings) == 1


def test_initialize_surfaces_project_config_warning(tmp_path) -> None:
    (tmp_path / '.delphi-lsp.toml').write_text(
        '[projects]\nincludes = ["*.dpr"]\n',
        encoding='utf-8',
    )
    server = create_server()
    handler = server.lsp.fm.features[INITIALIZE]
    params = SimpleNamespace(
        workspace_folders=None,
        root_uri=tmp_path.as_uri(),
        initialization_options={},
    )

    with mock.patch.object(server, 'show_message') as show_message:
        handler(params)

    assert show_message.call_count == 1
    assert 'Unsupported key' in show_message.call_args.args[0]
