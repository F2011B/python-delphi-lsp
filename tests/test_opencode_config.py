import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ollama_is_default_opencode_runtime() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    assert config['model'] == 'ollama/ornith-lspctx'
    assert config['small_model'] == 'ollama/ornith-lspctx'


def test_ollama_lsp_context_keeps_room_after_opencode_tool_prompt() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    lsp_model = config['provider']['ollama']['models']['ornith-lspctx']
    context_limit = lsp_model['limit']['context']
    conservative_opencode_prompt_overhead = 16_384

    assert lsp_model['id'] == 'ornith-lspctx:latest'
    assert context_limit == 131_072
    assert context_limit - conservative_opencode_prompt_overhead >= 100_000
    assert context_limit < config['provider']['ollama']['models']['ornith:latest']['limit']['context']


def test_readme_does_not_recommend_32k_vllm_for_opencode_lsp_runs() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')

    assert 'opencode run --dir . --model vllm/ornith-smallctx' not in readme
    assert 'opencode run --dir . --model ollama/ornith-lspctx' in readme
    assert '--agent vllm-lsp' in readme
    assert '--agent vllm-lsp-edit' in readme


def test_vllm_lsp_context_is_tool_enabled_for_opencode() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    vllm = config['provider']['vllm']
    lsp_model = vllm['models']['ornith-lspctx']
    context_limit = lsp_model['limit']['context']

    assert vllm['options']['baseURL'] == 'http://127.0.0.1:8001/v1'
    assert vllm['options']['apiKey'] == 'vllm'
    assert lsp_model['id'] == 'ornith-vllm-metal'
    assert lsp_model['tool_call'] is True
    assert context_limit == 44_352
    assert context_limit < vllm['models']['ornith-vllm-metal']['limit']['context']
    assert all(model['tool_call'] is True for model in vllm['models'].values())


def test_vllm_lsp_context_keeps_room_after_opencode_tool_prompt() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    lsp_model = config['provider']['vllm']['models']['ornith-lspctx']
    conservative_opencode_prompt_overhead = 16_384

    assert lsp_model['id'] == 'ornith-vllm-metal'
    assert lsp_model['limit']['context'] == 44_352
    assert lsp_model['limit']['context'] - conservative_opencode_prompt_overhead >= 16_384
    assert lsp_model['tool_call'] is True
    assert lsp_model['reasoning'] is False


def test_vllm_lsp_agent_keeps_only_lsp_tool_enabled() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    agent = config['agent']['vllm-lsp']
    tools = agent['tools']

    assert tools['lsp'] is True
    assert tools['bash'] is False
    assert tools['read'] is False
    assert tools['glob'] is False
    assert tools['grep'] is False
    assert tools['edit'] is False
    assert tools['write'] is False
    assert tools['task'] is False
    assert tools['webfetch'] is False
    assert tools['todowrite'] is False
    assert tools['skill'] is False


def test_delphi_lsp_config_uses_auto_discovery_by_default() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    initialization = config['lsp']['delphi']['initialization']
    assert initialization['autoDiscoverPaths'] is True
    assert 'includePaths' not in initialization
    assert 'defines' not in initialization


def test_vllm_lsp_edit_agent_keeps_only_lsp_and_edit_enabled() -> None:
    config = json.loads((ROOT / 'opencode.json').read_text(encoding='utf-8'))

    agent = config['agent']['vllm-lsp-edit']
    tools = agent['tools']

    assert tools['lsp'] is True
    assert tools['edit'] is True
    assert tools['bash'] is False
    assert tools['read'] is False
    assert tools['glob'] is False
    assert tools['grep'] is False
    assert tools['write'] is True
    assert tools['task'] is False
    assert tools['webfetch'] is False
    assert tools['todowrite'] is False
    assert tools['skill'] is False
