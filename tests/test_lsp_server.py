import inspect
from functools import partial
from types import SimpleNamespace
from unittest import mock

from lsprotocol.types import TEXT_DOCUMENT_DID_CHANGE, TextDocumentSyncKind

from delphi_lsp.lsp_server import create_server


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
