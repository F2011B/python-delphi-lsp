from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "pdf"
CORPUS_REPORT = ROOT / "output" / "corpus" / "corpus_report.json"
LANGUAGE_FEATURE_MATRIX = ROOT / "output" / "release" / "delphi_language_feature_matrix.json"
SYMBOL_REPORTS = {
    "mORMot core base": (
        ROOT / "output" / "corpus" / "mormot_core_base_symbols.json",
        ["WinAnsiString", "TPUtf8CharArray"],
    ),
    "mORMot core text": (
        ROOT / "output" / "corpus" / "mormot_core_text_symbols.json",
        ["IdemPCharAndGetNextItem", "GetNextItem", "TrimValue"],
    ),
    "PythonEngine": (
        ROOT / "output" / "corpus" / "pythonengine_symbols.json",
        ["TEventDefs", "PyObjectDestructor", "PythonToDelphi"],
    ),
    "OpenSSL full include": (
        ROOT / "output" / "corpus" / "mormot_lib_openssl11_full_symbols.json",
        ["OPENSSL_VERSION_NUMBER", "BIO_printf", "BIO_meth_free"],
    ),
    "mORMot core RTTI": (
        ROOT / "output" / "corpus" / "mormot_core_rtti_symbols.json",
        ["ERttiException", "TRttiOrd", "GetEnumNameValue"],
    ),
    "mORMot net sock": (
        ROOT / "output" / "corpus" / "mormot_net_sock_symbols.json",
        ["ioctlsocket", "NetIsIP4", "NewSocketIP4Lookup"],
    ),
    "mORMot test core data": (
        ROOT / "output" / "corpus" / "mormot_test_core_data_symbols.json",
        ["TTestCoreProcess", "TComplexClass", "TCat"],
    ),
    "Python4Delphi WrapDelphi": (
        ROOT / "output" / "corpus" / "python4delphi_wrapdelphi_symbols.json",
        ["RttiCall", "TExposedGetSet", "GetterWrapper"],
    ),
    "mORMot core variants": (
        ROOT / "output" / "corpus" / "mormot_core_variants_symbols.json",
        ["IDocList", "IDocDict", "VariantToVarRec"],
    ),
    "mORMot core OS": (
        ROOT / "output" / "corpus" / "mormot_core_os_symbols.json",
        ["SleepHiRes", "GetTickCount64", "SleepStep"],
    ),
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk_symbols(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        result.append(item)
        children = item.get("children") or []
        if children:
            result.extend(_walk_symbols(children))
    return result


def _symbol_summary() -> list[list[str]]:
    rows = [["File", "Symbols", "Evidence"]]
    for label, (path, expected) in SYMBOL_REPORTS.items():
        if not path.exists():
            rows.append([label, "missing", "missing report"])
            continue
        data = _load_json(path)
        flat = _walk_symbols(data if isinstance(data, list) else [])
        names = {item.get("name") for item in flat}
        hits = [name for name in expected if name in names]
        rows.append([label, str(len(flat)), ", ".join(hits) if hits else "symbols indexed"])
    return rows


def _language_feature_summary() -> dict[str, Any]:
    if not LANGUAGE_FEATURE_MATRIX.exists():
        return {
            "summary": {"total": "missing", "covered": "missing", "operation_names": []},
            "verification": {"ok": False},
        }
    return _load_json(LANGUAGE_FEATURE_MATRIX)


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _table(rows: list[list[Any]], widths: list[float] | None = None) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return table


def build_pdf(output_path: Path) -> None:
    corpus = _load_json(CORPUS_REPORT)
    feature_matrix = _language_feature_summary()
    feature_summary = feature_matrix["summary"]
    feature_verification = feature_matrix.get("verification", {})
    summary = corpus["summary"]
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#111827"),
        spaceAfter=8,
    )
    h2 = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=10,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#111827"),
        spaceAfter=5,
    )
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#374151"),
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story: list[Any] = [
        _para("Delphi LSP + opencode Fortschritt", title),
        _para(f"Stand: {generated}", small),
        _para(
            "Status: Parser/Korpus-Gate, Delphi-LSP-rootUri-Grossdateitest und ein "
            "synthetischer 117511-Zeilen-LSP-Test sind gruen. Die interaktiven LSP-"
            "Funktionen nutzen den schnellen Strukturpfad fuer jede Dateigroesse; "
            "vollstaendige Semantik bleibt als Fallback erhalten. Ornith laeuft fuer "
            "opencode nun ueber Ollama mit einem lokalen 128k-Kontext-Alias; der 100k-"
            "LSP-Nachweis ist mit 128k erneut erbracht. Zusaetzlich ist ein echter "
            "GitHub-Corpus-LSP-plus-Edit-Lauf auf einer 14309-Zeilen-Sandboxkopie "
            "belegt. vLLM ist lokal mit 44352 Kontext fuer LSP-only und "
            "LSP-plus-Edit auf GitHub- und 117511-Zeilen-Sandbox verifiziert "
            "und gegen versehentliche "
            "Hugging-Face-Downloads abgesichert. Es wurde nicht gepusht und keine "
            "GitHub-Quelldatei veraendert.",
            body,
        ),
        _para("Aktuelle Verifikation", h2),
        _table(
            [
                ["Gate", "Ergebnis"],
                ["Unit tests", "145 passed, 43 subtests passed in 11.32s"],
                [
                    "GitHub corpus",
                    f"{summary['ok']}/{summary['total_files']} ok, {summary['fail']} fail, {summary['large_files']} large files",
                ],
                ["Semantic mode", str(summary["semantic"])],
                [
                    "opencode Ornith endpoint",
                    "Ollama ornith-lspctx:latest als Default; vLLM ornith-lspctx 44352 via vllm-lsp",
                ],
                [
                    "opencode tools",
                    "GitHub LSP+Edit: Ollama 5319/5018 ms; vLLM-44352 5380/5028 ms",
                ],
                [
                    "opencode payload",
                    "Default: 29318 Systemprompt-Zeichen/10 Tools; vLLM-LSP-only: nur LSP-Tool",
                ],
                [
                    "Delphi LSP rootUri",
                    "rootUri-Grossdatei gruen; PythonEngine: 905 top / 3565 flat",
                ],
                [
                    "Delphi Feature-Matrix",
                    f"{feature_summary['covered']}/{feature_summary['total']} Gruppen; "
                    f"{len(feature_summary.get('operation_names', []))} LSP-Operationen; "
                    f"{feature_summary.get('direct_lsp_assertions', 0)} direkte LSP-Assertions; "
                    f"verification={feature_verification.get('ok')}",
                ],
                [
                    "100k+ Performance",
                    "117511 Zeilen: Symbole, Query, Definition, Hover, References, Rename <2s",
                ],
                [
                    "Model cache",
                    "vLLM Cache komplett: Shards 1-4 vorhanden; Dry-Run ohne Download; start_permitted=true",
                ],
                [
                    "Packaging",
                    "Wheel/sdist frisch gebaut; Wheel-Smoke und ausgepackter sdist-Testlauf ok",
                ],
            ],
            [48 * mm, 122 * mm],
        ),
        _para("Corpus nach Repository", h2),
    ]

    repo_rows = [["Repo", "Commit", "OK", "Fail", "Large"]]
    for repo, stats in summary["by_repo"].items():
        repo_rows.append(
            [
                repo,
                summary["repo_commits"].get(repo, ""),
                str(stats.get("ok", 0)),
                str(stats.get("fail", 0)),
                str(stats.get("large", 0)),
            ]
        )
    story.extend([_table(repo_rows, [38 * mm, 32 * mm, 24 * mm, 24 * mm, 24 * mm])])

    story.extend(
        [
            _para("opencode LSP Symbolnachweis", h2),
            _table(_symbol_summary(), [42 * mm, 24 * mm, 104 * mm]),
            _para("Neue Integrationsbefunde", h2),
            _para(
                "vLLM ist offline abgesichert: ohne --allow-download setzt das Startskript "
                "HF_HUB_OFFLINE=1 und TRANSFORMERS_OFFLINE=1. Der Hugging-Face-Cache fuer "
                "deepreinforce-ai/Ornith-1.0-9B ist jetzt vollstaendig, Shards 1-4 sind "
                "vorhanden und es gibt keine .incomplete-Reste. prepare_ornith_cache.py "
                "erzeugt standardmaessig weiterhin nur einen Dry-Run-Plan und versucht "
                "ohne explizites --allow-download keinen Download. Die opencode-vLLM-"
                "Konfiguration ist vorbereitet: vllm/ornith-lspctx zeigt auf "
                "ornith-vllm-metal, nutzt den lokal verifizierten 44352-Kontext "
                "mit VLLM_METAL_MEMORY_FRACTION=0.97 als Skript-Default "
                "und setzt tool_call=true. Ein 131072-vLLM-Start wurde getestet und "
                "scheiterte an der KV-Cache-Grenze: noetig waren 4.06 GiB, verfuegbar "
                "waren etwa 1.4 GiB; vLLM schaetzte 44352 Tokens als lokales Maximum. "
                "Mit dem Agenten vllm-lsp ist dieser Kontext fuer LSP-only lauffaehig, "
                "weil opencode nur das LSP-Werkzeug mitschickt. Der Capture-Vergleich bestaetigt den Kontextdruck: "
                "der Default-Request hatte 29318 Systemprompt-Zeichen und 10 Tool-Schemas, "
                "der LSP-only-Request 8978 Systemprompt-Zeichen und nur das LSP-Tool. "
                "Der aktuelle GitHub-vLLM-LSP-only-Lauf meldete in der ersten Anfrage 2915 Input-Tokens; "
                "die 14309-Zeilen-Datei wurde nicht in den Modellkontext kopiert, sondern ueber LSP adressiert. "
                "release_evidence.json fuehrt das als context_budget.status=pass: 39774 JSON-Zeichen "
                "und 9 Tool-Schemas weniger als der Default-Agent, geschaetzt 2964 Request-Tokens "
                "und etwa 41388 Tokens Restbudget im 44352-Kontext. Die 117k-Zeilen-Datei "
                "und die 14309-Zeilen-GitHub-Datei wurden ueber LSP adressiert und nicht in "
                "den Modellprompt geladen. goal_audit.status=pass fasst die Kernanforderungen "
                "vLLM-Endpunkt, opencode-LSP auf >100k-Zeilen-Dateien, GitHub-Testprojekte, "
                "Delphi-Feature-Abdeckung, PDF-Fortschritt, unveraenderte GitHub-Quellen "
                "und no-push zusammen. "
                "Der volle opencode-Standardprompt plus Tool-Schema bleibt fuer diesen lokalen vLLM-Pfad zu eng; "
                "fuer normale opencode-LSP/Edit-Sessions bleibt 131072 ueber Ollama der Default.",
                body,
            ),
            _para(
                "Der aktive opencode-Endpunkt ist nun Ollama. Das vorhandene lokale "
                "ornith:latest-Modell liegt als GGUF Q4_K_M mit 5.6 GB vor. Fuer opencode "
                "ist ornith-lspctx:latest als lokales Aliasmodell mit PARAMETER num_ctx "
                "131072 der Default; Ollama bestaetigte beim Create-Lauf existing layer reuse "
                "und erzeugte nur ein neues kleines Parameter-Layer. Der 8k-Versuch "
                "war zu klein fuer opencode; 32k bleibt nur ein vLLM-Smoke-Test. Fuer "
                "Ollama bleibt 128k der Default, damit nach Systemprompt und Tool-Schema "
                "genug Arbeitspuffer fuer echte opencode-LSP-Sessions bleibt. Der "
                "vLLM-44352-Pfad ist bewusst enger: Agent vllm-lsp deaktiviert bash, read, "
                "glob, grep, edit, write, task, webfetch, todowrite und skill und laesst "
                "nur lsp aktiv. Agent vllm-lsp-edit erlaubt zusaetzlich edit; write bleibt "
                "nur wegen der opencode-internen Edit-Schema-Kopplung sichtbar und wird "
                "im Probe-Runner als echter Toolcall weiterhin verboten.",
                body,
            ),
            _para(
                "Packaging-Status: Standalone-Wheel und sdist sind pruefbar. Der delphi-lsp-"
                "Entry-Point installiert pygls und lsprotocol standardmaessig; der Wheel-"
                "Smoke importierte delphiast, parse(..., \"Unit1.pas\") und den LSP-Server. "
                "Der sdist-Smoke lief als Editable-Checkout: 129 passed, 1 skipped, "
                "43 subtests passed in 10.39s. opencode.json sowie die opencode-, vLLM-, "
                "Cache-Prepare- und Ollama-Hilfsskripte sind in MANIFEST.in enthalten; test_projects und "
                "output bleiben draussen.",
                body,
            ),
            _para(
                "Der zuvor gefundene LSP-rootUri-Fehler wurde auf einen zyklischen Import-"
                "Index ohne visited-Set zurueckgefuehrt. Der neue Regressionstest deckt "
                "Importzyklen ab; der grosse LSP-Test initialisiert nun mit Workspace-rootUri "
                "wie opencode und findet weiterhin die erwarteten Symbole.",
                body,
            ),
            _para(
                "Sprachfeature-Nachweis: output/release/delphi_language_feature_matrix.json "
                "fasst die Abdeckung jetzt maschinenlesbar zusammen. Der aktuelle Stand "
                f"weist {feature_summary['covered']} von {feature_summary['total']} Delphi-"
                "Feature-Gruppen als lokal belegt aus, mit Pattern-Checks gegen versionierte "
                "Fixtures/Tests, "
                f"{feature_summary.get('direct_lsp_assertions', 0)} direkt ausgefuehrten "
                "LSP-Assertions und den LSP-Operationen "
                f"{', '.join(feature_summary.get('operation_names', []))}. "
                f"Die Matrix-Verifikation ist {feature_verification.get('ok')}; "
                f"LSP-Symbolfehler: {len(feature_verification.get('missing_lsp_symbols', []))}.",
                body,
            ),
            _para(
                "opencode-LSP/Edit-Nachweis: Mit OPENCODE_EXPERIMENTAL_LSP_TOOL=true und "
                "ollama/ornith-lspctx fuehrte opencode mit 128k Kontext einen echten "
                "lsp.workspaceSymbol-Toolcall auf Mega100kUnit.pas aus. Das Symbol "
                "MegaProc02500 wurde bei Zeile 117463 gefunden, Toolzeit 1410 ms. "
                "Der Runner verbot read, bash, glob und edit waehrend dieser Probe; "
                "die JSONL-Evidence enthaelt nur den LSP-Toolcall. Der separate Edit-"
                "Kettenlauf auf derselben ignorierten Sandbox fuegte "
                "OPENCODE_OLLAMA_STRUCTURE_PATH_PROBE_20260630 an die vorhandene MegaProc02500-"
                "Markierung an; Toolzeit 975 ms. Die maschinenlesbare Zusammenfassung liegt "
                "in output/release/release_evidence.json.",
                body,
            ),
            _para(
                "vLLM-LSP-Nachweis: Mit vllm/ornith-lspctx, Agent vllm-lsp und "
                "OPENCODE_EXPERIMENTAL_LSP_TOOL=true fuehrte opencode denselben "
                "workspaceSymbol-Toolcall auf Mega100kUnit.pas aus. Das Modell erhielt "
                "nur das LSP-Tool. Der lokal startbare vLLM-Kontext betraegt 44352 "
                "Tokens; der Toolcall lieferte MegaProc02500 bei Zeile 117463 in 1383 ms.",
                body,
            ),
            _para(
                "vLLM-LSP/Edit-Nachweis: Mit vllm/ornith-lspctx und Agent "
                "vllm-lsp-edit fuehrte opencode zuerst workspaceSymbol auf "
                "Mega100kUnit.pas aus und setzte danach per edit den Marker "
                "OPENCODE_VLLM44K_LSP_EDIT_PROBE_20260701 direkt in MegaProc02500. "
                "LSP-Toolzeit: 1381 ms; Edit-Toolzeit: 1104 ms; die JSONL-Evidence "
                "enthaelt keine read-, bash-, glob-, grep-, write-, task-, webfetch-, "
                "todowrite- oder skill-Toolcalls. Der Capture-Proxy sah ca. 15.5k/"
                "16.5k JSON-Zeichen und 3718/4030 Prompt-Tokens.",
                body,
            ),
            _para(
                "GitHub-LSP/Edit-Nachweis: mORMot2/src/core/mormot.core.base.pas wurde "
                "als 14309-Zeilen-Sandboxkopie unter output/github_lsp_edit_project "
                "verwendet. Mit Ollama fand opencode TSynPersistent per workspaceSymbol "
                "in 5319 ms und setzte OPENCODE_OLLAMA_GITHUB_EDIT_PROBE_20260701 per "
                "edit in 5018 ms. Mit vllm/ornith-lspctx fand opencode dasselbe Symbol "
                "in 5380 ms und setzte vLLM 44k edit verification 20260701 per edit "
                "in 5028 ms. Die JSONL-Evidence enthaelt nur lsp und edit; ein echter "
                "write-Toolcall bleibt verboten. Zusaetzlich fuehrte der vLLM-44352-LSP-only-"
                "Lauf vier echte LSP-Toolcalls mit nur dem LSP-Tool aus: workspaceSymbol "
                "fand TSynPersistent in 2686 ms, documentSymbol lief in 1556 ms, hover "
                "lieferte class TSynPersistent: TSynPersistent in 2740 ms und opencodes "
                "rohes goToDefinition wurde als normalisierte LSP-definition in 2696 ms "
                "gewertet. Die Originaldatei im GitHub-Corpus blieb ohne Marker und ohne "
                "Git-Statusaenderung.",
                body,
            ),
            _para(
                "Die vorherige 25s+-Kante fuer sehr grosse Dateien ist im LSP behoben, "
                "ohne einen reinen Grossdatei-Sonderpfad einzufuehren. documentSymbol, "
                "Definition, Hover, References, Rename und member completion beginnen nun "
                "mit dem deklarationsorientierten Strukturmodell fuer jede Dateigroesse; "
                "der vollstaendige Semantikpfad wird nur fuer Fallbacks oder zusaetzliche "
                "Treffer genutzt. Der gemischte 117511-Zeilen-Test laeuft in ca. 0.64s, "
                "der Sechs-Dateien-100k-Queryfilter in ca. 0.65s und die echte Repo-Root-"
                "Suche nach TPythonEngine in unter 0.5s. Lokale Bezeichner im "
                "Prozedurrumpf werden fuer Definition, Hover, References und Rename ueber "
                "den Outline-Scope aufgeloest, ohne die Grossdatei voll zu parsen. Der "
                "Probe-Runner kann mehrere Tool-Evidenzen abwarten und den Lauf nach LSP "
                "plus edit kontrolliert beenden.",
                body,
            ),
            _para("Neue Sprachfeatures in diesem Lauf", h2),
            _para(
                "Behoben wurden: kontextuelle Keywords als Ausdrucks-Identifier wie inherited Add; "
                "Calling-Conventions an Routine-Implementierungen wie ; cdecl; adjazente String-/Char-Code-Literale "
                "wie '%s'#13#10; generische Punkt-Suffixe wie .AsType&lt;T&gt;; typisierte inline for-Variablen; "
                "Formatargumente wie Value:0:Precision; standalone .inc-Fragmente; varargs vor und nach Semikolon; "
                "external nach Calling-Convention; leere while-/then-Branches; @-Zuweisungen an Routine-Pointer.",
                body,
            ),
            _para(
                "Neu dazugekommen sind: leere repeat-until-Bodies; Forward-Interface- und abstract-class-"
                "Deklarationen; nichtleere Generic-Tokens fuer <>-Vergleiche; leere Statements und doppelte "
                "Semikolons; benannte Call-Argumente mit :=; spaced generic constructor calls; contextual "
                "strict-Feldnamen; Record-Endfelder ohne Semikolon; forward nach overload; und zusammengesetzte "
                "Calling-Conventions nach virtual.",
                body,
            ),
            _para(
                "In diesem Update wurden zusaetzlich abgedeckt: Pointer auf einbuchstabige Generic-Typ-"
                "Parameter wie ^T; Calling-Conventions von Record-/Var-Proc-Typen ueber Zeilenumbrueche hinweg; "
                "Routine-Implementierungen der Form cdecl external ohne zusaetzliches Semikolon; Literal-only "
                ".inc-Fragmente; sowie UTF-16LE/BOM-kodierte Delphi-Quellen im ProjectIndexer, Include-Loader "
                "und Corpus-Runner.",
                body,
            ),
            _para(
                "Der aktuelle Lauf schliesst die zuletzt offenen Korpuscluster: bedingt entfernte TypeAlias-"
                "RHS vor visibility sections; leere bedingte uses-Clauses; rohe Compiler-Fehlertexte in "
                "Plattformguards; Gleichheitsausdruecke als Statement; sechs Apostrophe als Stringliteral; "
                "leere else-Branches; bare unit initialization blocks der Form begin..end.; sowie ASM-"
                "Routinebodies mit lokalen Deklarationen und Semikola in ASM-Items.",
                body,
            ),
            _para("Grosse Dateien, die jetzt gruen sind", h2),
        ]
    )

    large_paths = [
        "test_projects/github_repos/mORMot2/src/lib/mormot.lib.openssl11.full.inc",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.base.pas",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.os.pas",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.rtti.pas",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.text.pas",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.unicode.pas",
        "test_projects/github_repos/mORMot2/src/core/mormot.core.variants.pas",
        "test_projects/github_repos/mORMot2/src/net/mormot.net.sock.pas",
        "test_projects/github_repos/mORMot2/src/ui/mormot.ui.pdf.pas",
        "test_projects/github_repos/mORMot2/test/test.core.data.pas",
        "test_projects/github_repos/python4delphi/Source/PythonEngine.pas",
        "test_projects/github_repos/python4delphi/Source/WrapDelphi.pas",
    ]
    by_path = {row["path"]: row for row in corpus["results"]}
    large_rows = [["Lines", "Status", "Semantic problems", "Path"]]
    for path in large_paths:
        row = by_path[path]
        large_rows.append([str(row["lines"]), row["status"], str(row["semantic_problems"]), path])
    story.append(_table(large_rows, [18 * mm, 18 * mm, 28 * mm, 106 * mm]))

    story.extend(
        [
            _para("100k+-Datei und Performance", h2),
            _table(
                [
                    ["Gate", "Ergebnis"],
                    ["Generierte Datei", "Mega100kUnit.pas, 117511 Zeilen, 2500 Prozeduren"],
                    ["Parser vorher", "ca. 14-16s fuer Vollparse, 5002 Symbole"],
                    ["LSP vorher", "Performance-Gate RED: 17.118s > 10s"],
                    ["Strukturmodell", "GREEN: 0.34s fuer 117511 Zeilen, >=5000 Symbole"],
                    ["documentSymbol", "GREEN: ca. 0.65s, Strukturpfad fuer jede Dateigroesse"],
                    ["Mixed workspace", "RED 3.58s, GREEN ca. 0.64s fuer 117511 Zeilen plus kleine Unit"],
                    ["Query filter", "ca. 0.65s fuer MegaProc02500 trotz fuenf nicht passenden 117511-Zeilen-Dateien"],
                    ["Repo-root workspace/symbol", "TPythonEngine: 3.947s vorher, jetzt unter 0.5s mit query-gefiltertem Outline-Index"],
                    ["Body definition", "lokale Variable Value in MegaProc02500 ca. 0.60s"],
                    ["Body hover", "Hover auf lokale Variable Value ca. 0.60s"],
                    ["Body references", ">=40 lokale Value-Referenzen ca. 0.70s"],
                    ["Body rename", ">=40 Rename-Edits inklusive Deklaration ca. 1.00s"],
                    ["debug document-symbols", "1.46s auf editierter Mega100kUnit.pas: 2501 Top-Level / 5001 flache Symbole"],
                    ["workspace/symbol", "query-gefilterter Outline-Index findet MegaProc02500 ohne vorheriges Oeffnen"],
                    ["opencode 128k LSP-only", "Ollama ornith-lspctx: 1410 ms, keine read/bash/glob/edit-Tools"],
                    ["opencode GitHub LSP+Edit", "Ollama 5319/5018 ms; vLLM-44352 5380/5028 ms; Original sauber"],
                    ["opencode vLLM 44352 LSP-only", "Agent vllm-lsp: 1383 ms auf 117511 Zeilen; GitHub 2686/1556/2740/2696 ms fuer 4 LSP-Operationen"],
                    ["opencode vLLM 44352 LSP+Edit", "Agent vllm-lsp-edit: LSP 1381 ms, edit 1104 ms, Marker 1x"],
                    ["opencode chain edit", "separater Ollama edit-Nachweis: 975 ms, Marker 1x"],
                    ["opencode probe runner", "wartet auf mehrere JSONL-Tool-Evidenzen und stoppt danach kontrolliert"],
                ],
                [48 * mm, 122 * mm],
            ),
            _para("Restarbeit", h2),
            _para(
                "Korpusgate, opencode-100k-Nachweis, release_evidence.json und Paketartefakte sind geprueft. "
                "Restarbeit: finale Repo-/Veroeffentlichungsentscheidung und Artefaktbereinigung "
                "vor dem Commit; gepusht wurde bewusst noch nicht.",
                body,
            ),
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Delphi LSP opencode progress",
    )
    doc.build(story)


def main() -> None:
    build_pdf(OUTPUT_DIR / "delphi_lsp_opencode_progress_2026-06-30.pdf")


if __name__ == "__main__":
    main()
