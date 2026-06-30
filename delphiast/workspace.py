from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .consts import AttributeName
from .parser import DelphiParser
from .semantic import Scope, ScopeKind, SymbolIndex
from .semantic_builder import SemanticBuilder, SemanticModel


@dataclass
class WorkspaceSemanticResult:
    models: dict[str, SemanticModel]
    index: SymbolIndex


def build_workspace_semantics(
    sources: dict[str, str],
    *,
    include_paths: Iterable[str] = (),
    defines: Iterable[str] = (),
    preprocessor_options=None,
    collect_references: bool = True,
) -> WorkspaceSemanticResult:
    parser = DelphiParser(
        include_paths=include_paths,
        defines=defines,
        preprocessor_options=preprocessor_options,
    )
    roots: dict[str, object] = {}
    for file_name, text in sources.items():
        result = parser.parse(text, file_name, build_semantic=False)
        roots[file_name] = result.root

    index = SymbolIndex()
    scopes: dict[str, Scope] = {}
    for file_name, root in roots.items():
        unit_name = root.get_attribute(AttributeName.anName) or file_name
        unit_scope = Scope(kind=ScopeKind.UNIT, name=unit_name)
        index.register_unit(unit_name, unit_scope, index_symbols=False)
        scopes[file_name] = unit_scope

    builder = SemanticBuilder(collect_references=collect_references)
    for file_name, root in roots.items():
        builder.declare(root, index=index, unit_scope=scopes[file_name], reset_state=False)

    models: dict[str, SemanticModel] = {}
    for file_name, root in roots.items():
        model = builder.resolve(root, scopes[file_name])
        models[file_name] = model

    return WorkspaceSemanticResult(models=models, index=index)
