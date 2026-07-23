from __future__ import annotations

import pytest

from delphi_lsp.consts import AttributeName
from delphi_lsp.parser import DelphiParser
from delphi_lsp.parser_backend import ParserBackend, ParserMode


SOURCE = """unit BackendDemo;
interface
type
  TBackendDemo = class
  end;
implementation
end.
"""


def test_delphiast_is_the_default_backend() -> None:
    parser = DelphiParser()

    assert parser.backend is ParserBackend.DELPHIAST


def test_backend_and_mode_accept_enum_or_string_values() -> None:
    parser = DelphiParser(backend="lark", mode="strict")

    assert parser.backend is ParserBackend.LARK
    assert parser.mode is ParserMode.STRICT


def test_invalid_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported parser backend"):
        DelphiParser(backend="unknown")


def test_tolerant_delphiast_parse_does_not_call_lark(monkeypatch: pytest.MonkeyPatch) -> None:
    import delphi_lsp.parser as parser_module

    monkeypatch.setattr(
        parser_module,
        "_get_lark_parser",
        lambda: (_ for _ in ()).throw(AssertionError("legacy parser called")),
    )

    result = DelphiParser(mode=ParserMode.TOLERANT).parse(SOURCE, "BackendDemo.pas")

    assert result.root.get_attribute(AttributeName.anName) == "BackendDemo"


def test_explicit_lark_backend_remains_available() -> None:
    result = DelphiParser(backend=ParserBackend.LARK).parse(SOURCE, "BackendDemo.pas")

    assert result.root.get_attribute(AttributeName.anName) == "BackendDemo"


def test_strict_default_uses_bounded_legacy_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    import delphi_lsp.parser as parser_module

    calls = []
    original = parser_module._get_lark_parser

    def tracked():
        calls.append(True)
        return original()

    monkeypatch.setattr(parser_module, "_get_lark_parser", tracked)

    result = DelphiParser().parse(SOURCE, "BackendDemo.pas")

    assert result.root.get_attribute(AttributeName.anName) == "BackendDemo"
    assert calls == [True]


def test_strict_compatibility_never_falls_back_for_large_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import delphi_lsp.parser as parser_module

    monkeypatch.setattr(
        parser_module,
        "_get_lark_parser",
        lambda: (_ for _ in ()).throw(AssertionError("legacy parser called")),
    )
    large_source = SOURCE.replace("implementation", "implementation\n" + (" " * 40_000))

    result = DelphiParser().parse(large_source, "BackendDemo.pas")

    assert result.root.get_attribute(AttributeName.anName) == "BackendDemo"
