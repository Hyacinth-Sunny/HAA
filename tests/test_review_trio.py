"""评审三件套 + 图表/编译纪律参数化 测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from haa.review_trio import (
    COMPILE_MAX_ROUNDS,
    DISTINCTION_RANGE_THRESHOLD,
    FIGURE_MAX_RETRIES,
    generate_chart_with_discipline,
    load_wisdom,
    needs_standard_revision,
    render_revision_prompt,
    render_wisdom_injection,
    run_external_review,
)


# ==================== 1. 智慧库 ====================

def test_wisdom_files_exist():
    """10 条冷启动文件在位（批次12 §12 第 1 条）。"""
    wisdom_dir = Path(__file__).parent.parent / "data" / "review_wisdom"
    files = list(wisdom_dir.glob("*.md"))
    assert len(files) >= 10, f"expected >=10 wisdom files, got {len(files)}"


def test_load_wisdom_by_lens_and_type():
    w = load_wisdom("correctness", "systems")
    assert w is not None
    assert "rubric_patterns" in w or "common_critiques" in w


def test_load_wisdom_fallback_general():
    w = load_wisdom("quality", "bogus_type")
    assert w is not None  # 退化到 quality-general


def test_render_wisdom_injection():
    block = render_wisdom_injection("correctness", "systems")
    assert "⛓⛓⛓" in block
    assert block != ""
    assert render_wisdom_injection("bogus", "bogus") == ""


# ==================== 2. 第五路异家族 ====================

def test_external_review_skipped_without_model():
    result = run_external_review("test", config=None)
    assert result["skipped"] is True
    assert result["score"] is None


def test_external_review_with_model():
    """配置了异家族模型时走 litellm 通道。"""
    cfg = SimpleNamespace(
        llm=SimpleNamespace(sub_agent_model="openai/gpt-4o"))
    # Mock litellm
    with patch("litellm.completion") as mock_comp:
        mock_comp.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"score": 0.7, "verdict": "solid"}'))])
        result = run_external_review("test paper", config=cfg)
        assert result["score"] == 0.7
        assert result["skipped"] is False


def test_external_review_model_error_graceful():
    cfg = SimpleNamespace(
        llm=SimpleNamespace(sub_agent_model="bogus/model"))
    with patch("litellm.completion", side_effect=Exception("api error")):
        result = run_external_review("test", config=cfg)
        assert result["skipped"] is True  # 不崩溃


# ==================== 3. 区分度触发 ====================

def test_needs_revision_low_range():
    scores = [0.62, 0.64, 0.63]  # 极差 0.02 < 0.5
    assert needs_standard_revision(scores) is True


def test_needs_revision_low_std():
    scores = [0.70, 0.71, 0.70]  # std ~0.005 < 0.05
    assert needs_standard_revision(scores) is True


def test_no_revision_wide_range():
    scores = [0.3, 0.7, 0.9]  # 极差 0.6 > 0.5
    assert needs_standard_revision(scores) is False


def test_no_revision_single_score():
    assert needs_standard_revision([0.5]) is False


def test_revision_prompt_contains_diagnosis():
    prompt = render_revision_prompt([0.62, 0.64], "systems")
    assert "区分度" in prompt or "修订" in prompt
    assert "极差" in prompt


# ==================== 4. 图表纪律参数化 ====================

def test_figure_max_retries_is_2():
    assert FIGURE_MAX_RETRIES == 2  # §5 硬编码


def test_chart_discipline_success_first_try():
    with patch("haa.tools.visualization.generate_chart",
               return_value={"ok": True, "path": "/tmp/x.png"}):
        with patch("haa.tools.visualization.verify_chart",
                   return_value={"verified": True, "description": "ok"}):
            result = generate_chart_with_discipline(
                "code", "/tmp/x.png", "a chart")
            assert result["ok"] is True and result["attempts"] == 1


def test_chart_discipline_fail_then_success():
    call_count = {"verify": 0}
    def mock_verify(path, desc, config=None):
        call_count["verify"] += 1
        if call_count["verify"] == 1:
            return {"verified": False, "issues": "bad labels"}
        return {"verified": True, "description": "fixed"}
    with patch("haa.tools.visualization.generate_chart",
               return_value={"ok": True, "path": "/tmp/x.png"}):
        with patch("haa.tools.visualization.verify_chart", mock_verify):
            result = generate_chart_with_discipline("code", "/tmp/x.png", "chart")
            assert result["ok"] is True and result["attempts"] == 2


def test_chart_discipline_give_up_after_max():
    with patch("haa.tools.visualization.generate_chart",
               return_value={"ok": True, "path": "/tmp/x.png"}):
        with patch("haa.tools.visualization.verify_chart",
                   return_value={"verified": False, "issues": "always bad"}):
            result = generate_chart_with_discipline("code", "/tmp/x.png", "chart")
            assert result["ok"] is False
            assert result["attempts"] == FIGURE_MAX_RETRIES
            assert len(result["failure_list"]) == FIGURE_MAX_RETRIES
            assert "no moribund" in result["note"]  # 不触发濒死


# ==================== 5. 编译循环纪律 ====================

def test_compile_max_rounds_is_5():
    assert COMPILE_MAX_ROUNDS == 5  # §4 硬编码


def test_compile_with_discipline_success():
    from haa.review_trio import compile_with_discipline
    mock_result = SimpleNamespace(ok=True, pdf_path="/tmp/main.pdf", log="")
    with patch("haa.p3.latex_compiler.LatexCompiler") as mock_cls:
        mock_cls.return_value.compile.return_value = mock_result
        result = compile_with_discipline("/tmp/paper")
        assert result["ok"] is True
        assert result["known_errors"] == []


def test_compile_with_discipline_known_errors():
    from haa.review_trio import compile_with_discipline
    mock_result = SimpleNamespace(ok=False, pdf_path=None,
                                   log="! Undefined control sequence\n! Missing $")
    with patch("haa.p3.latex_compiler.LatexCompiler") as mock_cls:
        mock_cls.return_value.compile.return_value = mock_result
        result = compile_with_discipline("/tmp/paper", max_rounds=3)
        assert result["ok"] is False
        assert result["rounds"] == 3
        assert len(result["known_errors"]) >= 1
        assert "user adjudicates" in result["note"]  # 不静默不濒死


# ==================== 6. ReviewStage 集成验证 ====================

def test_review_stage_has_five_lenses():
    from haa.stages.review import ReviewStage
    lens_names = [l for l, _ in ReviewStage.lenses]
    assert "external" in lens_names
    assert len(lens_names) == 5  # 四路+第五路


def test_review_stage_wisdom_injection_reaches_prompt():
    """智慧库注入到达 system_prompt（需 mock agent 捕获）。"""
    # 此测试验证集成面——mock agent 检查 prompt 含智慧库标记
    from haa.stages.review import ReviewStage
    from haa.config import Config, StorageConfig
    cfg = Config(storage=StorageConfig(db_path="/tmp/t.db",
                                       campaigns_dir="/tmp/camps"))
    stage = ReviewStage(llm=None, config=cfg)
    captured_prompts = []

    class CapturingAgent:
        def run(self, prompt, *, system_prompt=None, **kw):
            captured_prompts.append(system_prompt or "")
            from haa.llm.agent_loop import AgentLoopResult
            return AgentLoopResult(content='{"score":0.7,"verdict":"ok"}')

    stage._make_agent_loop = lambda: CapturingAgent()
    # 跑一次（会跳过 external——无模型配置）
    camp = MagicMock()
    camp.id = "test"
    ctx = MagicMock()
    ctx.paper = {"abstract": "test", "intro": "test",
                 "background": "test", "method": "test"}
    ctx.brief = None
    ctx.extra = {"paper_type": "systems"}
    ctx.candidate = None
    try:
        stage.run(camp, ctx)
    except Exception:
        pass  # mock 上下文可能不完整，只需检查 prompt
    # 至少一个 prompt 应含智慧库标记
    wisdom_prompts = [p for p in captured_prompts if "⛓⛓⛓" in p]
    assert len(wisdom_prompts) >= 1, \
        f"wisdom injection not reaching prompts; got {len(captured_prompts)} prompts"
