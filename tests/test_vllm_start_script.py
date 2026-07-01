from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vllm_start_script_defaults_to_opencode_usable_context() -> None:
    script = (ROOT / 'scripts' / 'start_ornith_vllm.sh').read_text(encoding='utf-8')

    assert 'MAX_MODEL_LEN="${MAX_MODEL_LEN:-44352}"' in script
    assert 'MAX_NUM_SEQS="${MAX_NUM_SEQS:-1}"' in script
    assert 'VLLM_METAL_MEMORY_FRACTION="${VLLM_METAL_MEMORY_FRACTION:-0.97}"' in script


def test_vllm_start_script_enables_qwen_tool_calls_by_default() -> None:
    script = (ROOT / 'scripts' / 'start_ornith_vllm.sh').read_text(encoding='utf-8')

    assert 'ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-1}"' in script
    assert 'TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_xml}"' in script
    assert '--enable-auto-tool-choice' in script
    assert '--tool-call-parser "$TOOL_CALL_PARSER"' in script
