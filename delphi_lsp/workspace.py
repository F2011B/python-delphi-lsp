from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .consts import AttributeName
from .nodes import SyntaxNode
from .parser import DelphiParser
from .preprocessor import IncludeLoader, PreprocessedSource
from .semantic import Scope, ScopeKind, SymbolIndex
from .semantic_builder import SemanticBuilder, SemanticModel


@dataclass
class WorkspaceSemanticResult:
    models: dict[str, SemanticModel]
    index: SymbolIndex
    preprocessed: dict[str, PreprocessedSource] = field(default_factory=dict)


def build_workspace_semantics(
    sources: dict[str, str],
    *,
    include_paths: Iterable[str] = (),
    defines: Iterable[str] = (),
    include_loader: IncludeLoader | None = None,
    preprocessor_options=None,
    collect_references: bool = True,
) -> WorkspaceSemanticResult:
    parser = DelphiParser(
        include_paths=include_paths,
        defines=defines,
        include_loader=include_loader,
        preprocessor_options=preprocessor_options,
    )
    roots: dict[str, SyntaxNode] = {}
    preprocessed_sources: dict[str, PreprocessedSource] = {}
    for file_name, text in sources.items():
        result = parser.parse(text, file_name, build_semantic=False)
        roots[file_name] = result.root
        preprocessed_sources[file_name] = result.preprocessed

    return build_workspace_semantics_from_roots(
        roots,
        preprocessed_sources=preprocessed_sources,
        collect_references=collect_references,
    )


def build_workspace_semantics_from_roots(
    roots: dict[str, SyntaxNode],
    *,
    preprocessed_sources: Mapping[str, PreprocessedSource] | None = None,
    collect_references: bool = True,
) -> WorkspaceSemanticResult:
    """Build project semantics from syntax roots that have already been parsed."""

    index = SymbolIndex()
    scopes: dict[str, Scope] = {}
    for file_name, root in roots.items():
        unit_name = root.get_attribute(AttributeName.anName) or file_name
        unit_scope = Scope(kind=ScopeKind.UNIT, name=unit_name)
        index.register_unit(unit_name, unit_scope, index_symbols=False)
        scopes[file_name] = unit_scope

    source_maps = preprocessed_sources or {}
    builder = SemanticBuilder(
        collect_references=collect_references,
        source_maps=source_maps,
    )
    for file_name, root in roots.items():
        builder.declare(root, index=index, unit_scope=scopes[file_name], reset_state=False)

    models: dict[str, SemanticModel] = {}
    for file_name, root in roots.items():
        model = builder.resolve(root, scopes[file_name])
        models[file_name] = model

    return WorkspaceSemanticResult(
        models=models,
        index=index,
        preprocessed=dict(source_maps),
    )
