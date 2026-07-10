from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'generate_release_evidence.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('generate_release_evidence', SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_evidence_paths_use_portable_posix_separators() -> None:
    module = _load_module()

    assert module._portable_evidence_path(
        PureWindowsPath(r"test_projects\github_repos\mORMot2\src\core\mormot.core.base.pas")
    ) == "test_projects/github_repos/mORMot2/src/core/mormot.core.base.pas"


def test_build_release_evidence_summarizes_corpus_opencode_and_packaging(tmp_path) -> None:
    module = _load_module()
    corpus_dir = tmp_path / 'output' / 'corpus'
    chain_dir = tmp_path / 'output' / 'mega_lsp_chain_project'
    github_dir = tmp_path / 'output' / 'github_lsp_edit_project'
    release_dir = tmp_path / 'output' / 'release'
    pdf_dir = tmp_path / 'output' / 'pdf'
    dist_dir = tmp_path / 'dist'
    github_source = tmp_path / 'test_projects' / 'github_repos' / 'mORMot2' / 'src' / 'core'
    hf_home = tmp_path / '.cache' / 'huggingface'
    repo_dir = hf_home / 'hub' / 'models--deepreinforce-ai--Ornith-1.0-9B'
    revision = 'abc123'
    snapshot_dir = repo_dir / 'snapshots' / revision
    corpus_dir.mkdir(parents=True)
    chain_dir.mkdir(parents=True)
    github_dir.mkdir(parents=True)
    release_dir.mkdir(parents=True)
    pdf_dir.mkdir(parents=True)
    github_source.mkdir(parents=True)
    dist_dir.mkdir()
    (repo_dir / 'refs').mkdir(parents=True)
    snapshot_dir.mkdir(parents=True)
    incomplete_blob = repo_dir / 'blobs' / 'partial.incomplete'
    incomplete_blob.parent.mkdir()
    incomplete_blob.write_bytes(b'partial')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'start_ornith_vllm.sh').write_text(
        '\n'.join(
                [
                    'MAX_MODEL_LEN="${MAX_MODEL_LEN:-44352}"',
                    'MAX_NUM_SEQS="${MAX_NUM_SEQS:-1}"',
                    'VLLM_METAL_MEMORY_FRACTION="${VLLM_METAL_MEMORY_FRACTION:-0.97}"',
                    'ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-1}"',
                    'TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_xml}"',
                ]
        ),
        encoding='utf-8',
    )
    (repo_dir / 'refs' / 'main').write_text(revision, encoding='utf-8')
    (snapshot_dir / 'model.safetensors.index.json').write_text(
        json.dumps(
            {
                'weight_map': {
                    'layer.1': 'model-00001-of-00004.safetensors',
                    'layer.2': 'model-00002-of-00004.safetensors',
                    'layer.3': 'model-00003-of-00004.safetensors',
                    'layer.4': 'model-00004-of-00004.safetensors',
                }
            }
        ),
        encoding='utf-8',
    )
    (snapshot_dir / 'model-00001-of-00004.safetensors').write_bytes(b'weights')
    (corpus_dir / 'corpus_report.json').write_text(
        json.dumps(
            {
                'summary': {
                    'total_files': 2,
                    'ok': 2,
                    'fail': 0,
                    'large_files': 1,
                    'semantic': True,
                }
            }
        ),
        encoding='utf-8',
    )
    mega_lines = [
        'procedure MegaProc02500; // OPENCODE_OLLAMA_STRUCTURE_PATH_PROBE_20260630',
        '  // OPENCODE_VLLM_LSP_EDIT_PROBE_20260701',
        '  // OPENCODE_VLLM44K_LSP_EDIT_PROBE_20260701',
    ]
    mega_lines.extend('// filler' for _ in range(100_001))
    (chain_dir / 'Mega100kUnit.pas').write_text('\n'.join(mega_lines) + '\n', encoding='utf-8')
    (chain_dir / 'opencode_lsp_probe_100k_ollama_128k_lsp_only.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'error',
                                'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                                'error': 'missing schema keys',
                                'time': {'start': 900, 'end': 901},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                                'output': '[{"name":"MegaProc02500"}]',
                                'time': {'start': 1000, 'end': 2372},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (chain_dir / 'opencode_lsp_edit_chain_100k_ollama_32k.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {'operation': 'workspaceSymbol', 'query': 'MegaProc02500'},
                                'output': '[{"name":"MegaProc02500"}]',
                                'time': {'start': 1000, 'end': 1322},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'edit',
                            'state': {
                                'status': 'completed',
                                'input': {'filePath': 'Mega100kUnit.pas'},
                                'output': 'Edit applied successfully.',
                                'time': {'start': 2000, 'end': 2975},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (chain_dir / 'opencode_lsp_probe_100k_vllm_44k_lsp_only_agent.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'workspaceSymbol',
                                    'filePath': 'Mega100kUnit.pas',
                                    'line': 1,
                                    'character': 1,
                                    'query': 'MegaProc02500',
                                },
                                'output': '[{"name":"MegaProc02500"}]',
                                'time': {'start': 3000, 'end': 4388},
                            },
                        },
                    }
                )
            ]
        ),
        encoding='utf-8',
    )
    (chain_dir / 'opencode_lsp_edit_chain_100k_vllm_44k_lsp_edit_agent.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'workspaceSymbol',
                                    'filePath': 'Mega100kUnit.pas',
                                    'line': 1,
                                    'character': 1,
                                    'query': 'MegaProc02500',
                                },
                                'output': '[{"name":"MegaProc02500"}]',
                                'time': {'start': 5000, 'end': 6381},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'edit',
                            'state': {
                                'status': 'completed',
                                'input': {'filePath': 'Mega100kUnit.pas'},
                                'output': 'Edit applied successfully.',
                                'time': {'start': 7000, 'end': 8104},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (release_dir / 'opencode_request_payloads.json').write_text(
        json.dumps(
            {
                'default_agent': {
                    'json_chars': 51_627,
                    'system_chars': 29_318,
                    'tool_count': 10,
                    'tool_names': [
                        'bash',
                        'edit',
                        'glob',
                        'grep',
                        'read',
                        'skill',
                        'task',
                        'todowrite',
                        'webfetch',
                        'write',
                    ],
                },
                'vllm_lsp_only_agent': {
                    'json_chars': 11_853,
                    'system_chars': 8_978,
                    'tool_count': 1,
                    'tool_names': ['lsp'],
                },
                'vllm_lsp_edit_agent_first_request': {
                    'json_chars': 15_506,
                    'system_chars': 8_978,
                    'tool_count': 3,
                    'tool_names': ['edit', 'lsp', 'write'],
                },
            }
        ),
        encoding='utf-8',
    )
    (release_dir / 'delphi_language_feature_matrix.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'summary': {
                    'total': 20,
                    'covered': 20,
                    'lsp_operations': 8,
                    'direct_lsp_assertions': 21,
                    'operation_names': [
                        'completion',
                        'definition',
                        'documentSymbol',
                        'edit',
                        'hover',
                        'references',
                        'rename',
                        'workspaceSymbol',
                    ],
                },
                'verification': {
                    'ok': True,
                    'total': 20,
                    'covered': 20,
                    'missing_files': [],
                    'missing_patterns': [],
                    'lsp_assertions_checked': 21,
                    'missing_lsp_symbols': [],
                },
            }
        ),
        encoding='utf-8',
    )
    (github_source / 'mormot.core.base.pas').write_text(
        '  TSynPersistent = class(TObject)\n'
        '  public\n'
        '    constructor Create; overload; virtual;\n',
        encoding='utf-8',
    )
    (github_dir / 'mormot.core.base.pas').write_text(
        '  TSynPersistent = class(TObject)\n'
        '    // OPENCODE_OLLAMA_GITHUB_EDIT_PROBE_20260701\n'
        '    // OPENCODE_VLLM_GITHUB_EDIT_PROBE_20260701\n'
        '  /// vLLM 44k edit verification 20260701\n'
        '  public\n'
        '    constructor Create; overload; virtual;\n',
        encoding='utf-8',
    )
    (github_dir / 'opencode_lsp_edit_mormot_core_base_ollama_128k.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'workspaceSymbol',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 1,
                                    'character': 1,
                                    'query': 'TSynPersistent',
                                },
                                'output': '[{"name":"TSynPersistent"}]',
                                'time': {'start': 10000, 'end': 10420},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'edit',
                            'state': {
                                'status': 'completed',
                                'input': {'filePath': 'mormot.core.base.pas'},
                                'output': 'Edit applied successfully.',
                                'time': {'start': 11000, 'end': 11750},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (github_dir / 'opencode_lsp_edit_mormot_core_base_vllm_44k.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'workspaceSymbol',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 1,
                                    'character': 1,
                                    'query': 'TSynPersistent',
                                },
                                'output': '[{"name":"TSynPersistent"}]',
                                'time': {'start': 12000, 'end': 17380},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'edit',
                            'state': {
                                'status': 'completed',
                                'input': {'filePath': 'mormot.core.base.pas'},
                                'output': 'Edit applied successfully.',
                                'time': {'start': 18000, 'end': 23028},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (github_dir / 'opencode_lsp_ops_mormot_core_base_vllm_44k.jsonl').write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'workspaceSymbol',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 1,
                                    'character': 1,
                                    'query': 'TSynPersistent',
                                },
                                'output': '[{"name":"TSynPersistent"}]',
                                'time': {'start': 14000, 'end': 14450},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'documentSymbol',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 1,
                                    'character': 1,
                                },
                                'output': '[{"name":"TSynPersistent","kind":5}]',
                                'time': {'start': 15000, 'end': 15520},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'hover',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 553,
                                    'character': 4,
                                },
                                'output': 'TSynPersistent = class(TObject)',
                                'time': {'start': 16000, 'end': 16180},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'tool_use',
                        'part': {
                            'tool': 'lsp',
                            'state': {
                                'status': 'completed',
                                'input': {
                                    'operation': 'goToDefinition',
                                    'filePath': 'mormot.core.base.pas',
                                    'line': 577,
                                    'character': 31,
                                },
                                'output': '[{"name":"TSynPersistent","uri":"mormot.core.base.pas"}]',
                                'time': {'start': 17000, 'end': 17210},
                            },
                        },
                    }
                ),
            ]
        ),
        encoding='utf-8',
    )
    (dist_dir / 'python_delphi_lsp-2.0.0-py3-none-any.whl').write_text('wheel', encoding='utf-8')
    (dist_dir / 'python_delphi_lsp-2.0.0.tar.gz').write_text('sdist', encoding='utf-8')
    (pdf_dir / 'delphi_lsp_opencode_progress_2026-06-30.pdf').write_bytes(b'%PDF-1.4\n')
    (tmp_path / 'opencode.json').write_text(
        json.dumps(
            {
                'model': 'ollama/ornith-lspctx',
                'provider': {
                    'ollama': {
                        'models': {
                            'ornith-lspctx': {
                                'id': 'ornith-lspctx:latest',
                                'tool_call': True,
                                'limit': {'context': 131_072},
                            }
                        },
                    },
                    'vllm': {
                        'options': {'baseURL': 'http://127.0.0.1:8001/v1'},
                        'models': {
                            'ornith-lspctx': {
                                'id': 'ornith-vllm-metal',
                                'tool_call': True,
                                'limit': {'context': 44_352},
                            },
                            'ornith-smallctx': {
                                'id': 'ornith-vllm-metal',
                                'tool_call': True,
                                'limit': {'context': 32_768},
                            }
                        },
                    }
                },
                'agent': {
                    'vllm-lsp': {
                        'tools': {
                            'lsp': True,
                            'bash': False,
                            'read': False,
                            'glob': False,
                            'grep': False,
                            'edit': False,
                            'write': False,
                            'task': False,
                            'webfetch': False,
                            'todowrite': False,
                            'skill': False,
                        },
                    },
                    'vllm-lsp-edit': {
                        'tools': {
                            'lsp': True,
                            'edit': True,
                            'bash': False,
                            'read': False,
                            'glob': False,
                            'grep': False,
                            'write': True,
                            'task': False,
                            'webfetch': False,
                            'todowrite': False,
                            'skill': False,
                        },
                    },
                }
            }
        ),
        encoding='utf-8',
    )

    evidence = module.build_release_evidence(tmp_path, hf_home=hf_home)

    serialized_evidence = json.dumps(evidence, sort_keys=True)
    assert str(tmp_path) not in serialized_evidence
    assert evidence['packaging']['wheel']['path'] == (
        'dist/python_delphi_lsp-2.0.0-py3-none-any.whl'
    )
    assert evidence['packaging']['sdist']['path'] == 'dist/python_delphi_lsp-2.0.0.tar.gz'
    assert evidence['vllm']['hf_home'] == '@external/huggingface-cache'
    assert evidence['vllm']['cache_prepare']['hf_home'] == '@external/huggingface-cache'
    assert evidence['vllm']['cache_prepare']['cache_dir'] == '@external/huggingface-cache/hub'
    assert evidence['vllm']['incomplete_files'] == [
        {
            'path': (
                '@external/huggingface-cache/hub/'
                'models--deepreinforce-ai--Ornith-1.0-9B/blobs/partial.incomplete'
            ),
            'size': 7,
        }
    ]

    assert evidence['corpus']['ok'] == 2
    assert evidence['corpus']['fail'] == 0
    assert evidence['opencode']['model'] == 'ollama/ornith-lspctx'
    assert evidence['opencode']['context'] == 131_072
    assert evidence['opencode']['lsp_jsonl'].endswith('opencode_lsp_probe_100k_ollama_128k_lsp_only.jsonl')
    assert evidence['opencode']['edit_jsonl'].endswith('opencode_lsp_edit_chain_100k_ollama_32k.jsonl')
    assert evidence['opencode']['forbidden_tools'] == ['read', 'bash', 'glob', 'edit']
    assert evidence['opencode']['forbidden_tools_seen'] == []
    assert evidence['opencode']['lsp_only'] is True
    assert evidence['opencode']['lsp_elapsed_ms'] == 1372
    assert evidence['opencode']['edit_elapsed_ms'] == 975
    assert evidence['opencode']['marker_count'] == 1
    assert evidence['opencode']['request_payloads']['default_agent']['system_chars'] == 29_318
    assert evidence['opencode']['request_payloads']['default_agent']['tool_count'] == 10
    assert evidence['opencode']['request_payloads']['vllm_lsp_only_agent']['tool_names'] == ['lsp']
    assert evidence['language_features']['summary']['total'] == 20
    assert evidence['language_features']['summary']['covered'] == 20
    assert evidence['language_features']['summary']['lsp_operations'] == 8
    assert evidence['language_features']['summary']['direct_lsp_assertions'] == 21
    assert evidence['language_features']['verification']['ok'] is True
    assert evidence['language_features']['verification']['lsp_assertions_checked'] == 21
    assert evidence['language_features']['verification']['missing_lsp_symbols'] == []
    assert evidence['github_lsp_edit']['agent'] == 'vllm-lsp-edit'
    assert evidence['github_lsp_edit']['model'] == 'ollama/ornith-lspctx'
    assert evidence['github_lsp_edit']['source_line_count'] == 3
    assert evidence['github_lsp_edit']['sandbox_line_count'] == 6
    assert evidence['github_lsp_edit']['marker_count'] == 1
    assert evidence['github_lsp_edit']['source_marker_count'] == 0
    assert evidence['github_lsp_edit']['source_clean'] is True
    assert evidence['github_lsp_edit']['lsp_edit_only'] is True
    assert evidence['github_lsp_edit']['lsp_elapsed_ms'] == 420
    assert evidence['github_lsp_edit']['edit_elapsed_ms'] == 750
    assert evidence['github_lsp_edit']['forbidden_tools_seen'] == []
    assert evidence['github_vllm_lsp_edit']['agent'] == 'vllm-lsp-edit'
    assert evidence['github_vllm_lsp_edit']['model'] == 'vllm/ornith-lspctx'
    assert evidence['github_vllm_lsp_edit']['source_line_count'] == 3
    assert evidence['github_vllm_lsp_edit']['sandbox_line_count'] == 6
    assert evidence['github_vllm_lsp_edit']['marker_count'] == 1
    assert evidence['github_vllm_lsp_edit']['source_marker_count'] == 0
    assert evidence['github_vllm_lsp_edit']['source_clean'] is True
    assert evidence['github_vllm_lsp_edit']['lsp_edit_only'] is True
    assert evidence['github_vllm_lsp_edit']['lsp_elapsed_ms'] == 5380
    assert evidence['github_vllm_lsp_edit']['edit_elapsed_ms'] == 5028
    assert evidence['github_vllm_lsp_edit']['forbidden_tools_seen'] == []
    assert evidence['github_vllm_lsp_operations']['agent'] == 'vllm-lsp'
    assert evidence['github_vllm_lsp_operations']['model'] == 'vllm/ornith-lspctx'
    assert evidence['github_vllm_lsp_operations']['source_clean'] is True
    assert evidence['github_vllm_lsp_operations']['lsp_only'] is True
    assert evidence['github_vllm_lsp_operations']['forbidden_tools_seen'] == []
    assert evidence['github_vllm_lsp_operations']['operations_seen'] == [
        'workspaceSymbol',
        'documentSymbol',
        'hover',
        'definition',
    ]
    assert evidence['github_vllm_lsp_operations']['elapsed_ms_by_operation'] == {
        'workspaceSymbol': 450,
        'documentSymbol': 520,
        'hover': 180,
        'definition': 210,
    }
    assert evidence['vllm']['endpoint'] == 'http://127.0.0.1:8001/v1'
    assert evidence['vllm']['served_model_name'] == 'ornith-vllm-metal'
    assert evidence['vllm']['opencode_model'] == 'vllm/ornith-lspctx'
    assert evidence['vllm']['opencode_context'] == 44_352
    assert evidence['vllm']['opencode_tool_call'] is True
    assert evidence['vllm']['opencode_lsp_agent'] == 'vllm-lsp'
    assert evidence['vllm']['opencode_lsp_model'] == 'vllm/ornith-lspctx'
    assert evidence['vllm']['opencode_lsp_context'] == 44_352
    assert evidence['vllm']['opencode_lsp_only'] is True
    assert evidence['vllm']['opencode_lsp_elapsed_ms'] == 1388
    assert evidence['vllm']['opencode_lsp_input']['line'] == 1
    assert evidence['vllm']['opencode_lsp_forbidden_tools_seen'] == []
    assert evidence['vllm']['opencode_lsp_edit_agent'] == 'vllm-lsp-edit'
    assert evidence['vllm']['opencode_lsp_edit_model'] == 'vllm/ornith-lspctx'
    assert evidence['vllm']['opencode_lsp_edit_context'] == 44_352
    assert evidence['vllm']['opencode_lsp_edit_only'] is True
    assert evidence['vllm']['opencode_lsp_edit_forbidden_tools_seen'] == []
    assert evidence['vllm']['opencode_lsp_edit_elapsed_ms'] == 1104
    assert evidence['vllm']['opencode_lsp_edit_lsp_elapsed_ms'] == 1381
    assert evidence['vllm']['opencode_lsp_edit_marker_count'] == 1
    assert evidence['vllm']['start_defaults'] == {
        'enable_auto_tool_choice': True,
        'max_model_len': 44_352,
        'max_num_seqs': 1,
        'metal_memory_fraction': 0.97,
        'tool_call_parser': 'qwen3_xml',
    }
    assert evidence['context_budget'] == {
        'status': 'pass',
        'model': 'vllm/ornith-lspctx',
        'agent': 'vllm-lsp',
        'context_tokens': 44_352,
        'default_request': {
            'json_chars': 51_627,
            'system_chars': 29_318,
            'tool_count': 10,
            'tool_names': [
                'bash',
                'edit',
                'glob',
                'grep',
                'read',
                'skill',
                'task',
                'todowrite',
                'webfetch',
                'write',
            ],
        },
        'lsp_only_request': {
            'json_chars': 11_853,
            'system_chars': 8_978,
            'tool_count': 1,
            'tool_names': ['lsp'],
        },
        'request_json_chars_saved': 39_774,
        'system_chars_saved': 20_340,
        'tools_removed': 9,
        'estimated_lsp_only_request_tokens': 2_964,
        'estimated_context_tokens_remaining': 41_388,
        'large_file_line_count': 100_004,
        'github_file_line_count': 3,
        'lsp_only': True,
        'source_file_loaded_into_prompt': False,
    }
    failed_requirements = [
        item for item in evidence['goal_audit']['requirements'] if item['status'] != 'pass'
    ]
    assert failed_requirements == []
    assert evidence['goal_audit']['status'] == 'pass'
    requirements = {item['id']: item for item in evidence['goal_audit']['requirements']}
    assert requirements['ornith_vllm_endpoint']['status'] == 'pass'
    assert requirements['opencode_lsp_large_files']['status'] == 'pass'
    assert requirements['github_test_projects']['status'] == 'pass'
    assert requirements['all_delphi_language_features']['status'] == 'pass'
    assert requirements['smaller_context_via_lsp']['status'] == 'pass'
    assert requirements['pdf_progress']['status'] == 'pass'
    assert requirements['no_github_source_changes']['status'] == 'pass'
    assert requirements['no_push']['status'] == 'pass'
    assert evidence['vllm']['offline_only'] is True
    assert evidence['vllm']['cache_complete'] is False
    assert evidence['vllm']['start_permitted'] is False
    assert evidence['vllm']['present_shards'] == ['model-00001-of-00004.safetensors']
    assert evidence['vllm']['missing_shards'] == [
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ]
    assert evidence['vllm']['cache_prepare']['download_permitted'] is False
    assert evidence['vllm']['cache_prepare']['download_attempted'] is False
    assert evidence['vllm']['cache_prepare']['complete_before'] is False
    assert evidence['vllm']['cache_prepare']['complete_after'] is False
    assert evidence['vllm']['cache_prepare']['allow_patterns'] == [
        'model-00002-of-00004.safetensors',
        'model-00003-of-00004.safetensors',
        'model-00004-of-00004.safetensors',
    ]
    assert evidence['packaging']['wheel']['exists'] is True
    assert evidence['packaging']['sdist']['exists'] is True
