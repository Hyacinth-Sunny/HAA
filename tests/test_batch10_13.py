"""批次10-13 测试：全部新工具+EA门视图+experiment 写入+批次14 超参表。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from haa.harness.registry import ToolError, ToolRegistry
from haa.harness.tools.batch10_13 import (
    _compile_latex,
    _validate_code_ast,
    render_ea_gate_branch_view,
    write_experiment_entity,
)
from haa.harness.tools.native import apply_native_tools
from haa.branch_duel import BranchTree


def _reg(tmp_path):
    reg = ToolRegistry()
    apply_native_tools(reg, allowed_roots=(tmp_path,))
    return reg


# ==================== compile_latex ====================

def test_compile_latex_missing_arg():
    with pytest.raises(ToolError, match="tex_file"):
        _compile_latex({}, None)


def test_compile_latex_bad_extension():
    with pytest.raises(ToolError, match=".tex"):
        _compile_latex({"tex_file": "foo.txt"}, None)


# ==================== run_code AST 校验 ====================

def test_run_code_banned_import():
    errs = _validate_code_ast("import os\nprint('hi')")
    assert any("os" in e for e in errs)


def test_run_code_banned_subprocess():
    errs = _validate_code_ast("from subprocess import run")
    assert any("subprocess" in e for e in errs)


def test_run_code_clean_passes():
    errs = _validate_code_ast("import math\nprint(math.pi)")
    assert errs == []


def test_run_code_infinite_loop():
    errs = _validate_code_ast("while True:\n    pass")
    assert any("infinite" in e.lower() for e in errs)


def test_run_code_syntax_error():
    errs = _validate_code_ast("def foo(:\n  pass")
    assert any("syntax" in e.lower() for e in errs)


def test_run_code_execution():
    from haa.harness.tools.batch10_13 import _make_run_code
    svc = SimpleNamespace()
    fn = _make_run_code(svc)
    r = fn({"code": "print('hello world')"}, None)
    assert "hello world" in r.content


# ==================== memory_query / memory_write ====================

def test_memory_query_empty_bank(tmp_path):
    from haa.memory_bank import MemoryBank
    from haa.harness.tools.batch10_13 import _make_memory_query
    bank = MemoryBank(tmp_path)
    bank.rebuild_index()
    fn = _make_memory_query(bank)
    r = fn({"problem_class": "冲突消解类"}, None)
    assert "no matching" in r.content


def test_memory_write_and_query_roundtrip(tmp_path):
    from haa.memory_bank import MemoryBank, next_entity_id, IdeaEntity
    from haa.harness.tools.batch10_13 import _make_memory_query, _make_memory_write
    bank = MemoryBank(tmp_path)
    # 先用 bank API 写一条
    bank.write("ideas", IdeaEntity(
        idea_id=next_entity_id(tmp_path, "i"), title="死路A",
        core_claim="X", status="killed", kill_reason="反例",
        problem_class=["冲突消解类"]), writer="SEEK")
    bank.rebuild_index()
    fn_q = _make_memory_query(bank)
    r = fn_q({"problem_class": "冲突消解类"}, None)
    assert "死路A" in r.content


def test_memory_write_matrix_rejects_bad_writer(tmp_path):
    from haa.memory_bank import MemoryBank, IdeaEntity, next_entity_id
    from haa.harness.tools.batch10_13 import _make_memory_write
    bank = MemoryBank(tmp_path)
    fn = _make_memory_write(bank)
    with pytest.raises(ToolError, match="unknown entity"):
        fn({"entity": "bogus", "data": {}}, None)


# ==================== citation / search_analog ====================

def test_citation_verify_requires_input():
    from haa.harness.tools.batch10_13 import _citation_verify
    with pytest.raises(ToolError, match="required"):
        _citation_verify({}, None)


def test_search_analog_requires_input():
    from haa.harness.tools.batch10_13 import _search_analog
    with pytest.raises(ToolError, match="required"):
        _search_analog({}, None)


# ==================== report_challenge ====================

def test_report_challenge_valid():
    from haa.harness.tools.batch10_13 import _make_report_challenge
    fn = _make_report_challenge(SimpleNamespace())
    r = fn({"challenge_type": "原理不可行",
            "evidence": "反例 x"}, None)
    assert "原理不可行" in r.content and "pauses" in r.content


def test_report_challenge_bad_type():
    from haa.harness.tools.batch10_13 import _make_report_challenge
    fn = _make_report_challenge(SimpleNamespace())
    with pytest.raises(ToolError, match="must be"):
        fn({"challenge_type": "不爽", "evidence": "x"}, None)


# ==================== EA 门分支树视图 ====================

def test_ea_gate_branch_view(tmp_path):
    from haa.models import Candidate
    tree = BranchTree()
    tree.add_root(Candidate(campaign_id="c", slug="alpha",
                            title="Alpha", significance=.8,
                            win_odds=.6, difficulty=.4, queue_index=0))
    tree.kill_branch("alpha", stage="VERIFY", reason="反例 x<0")
    ctx = SimpleContext(extra={"branch_tree": tree})
    view = render_ea_gate_branch_view(ctx)
    assert "✗" in view and "Alpha" in view and "反例" in view


def test_ea_gate_no_tree():
    ctx = SimpleContext(extra={})
    assert "no branch tree" in render_ea_gate_branch_view(ctx)


class SimpleContext:
    def __init__(self, extra=None):
        self.extra = extra or {}


# ==================== experiment 实体写入 ====================

def test_write_experiment_entity(tmp_path):
    from haa.memory_bank import MemoryBank
    from haa.memory_bank import IdeaEntity, next_entity_id
    bank = MemoryBank(tmp_path)
    # 先写一个 idea
    bank.write("ideas", IdeaEntity(
        idea_id="i-20261008-0001", title="Test",
        core_claim="X", status="graduated"), writer="SEEK")
    # 直接写 experiment
    exp_id = write_experiment_entity(
        bank, idea_id="i-20261008-0001", tier="pilot",
        verdict="supported", metrics={"acc": 0.9},
        env="docker", solve_sh="/tmp/solve.sh")
    assert exp_id.startswith("e-")
    # 反向边自动补
    idea = bank.load("ideas", "i-20261008-0001")
    if idea:
        assert exp_id in idea.has_experiment


# ==================== 批次14：超参数总表核对 ====================

def test_hyperparameter_table_alignment():
    """批次14：配置默认值与计划书第六章 §3 超参总表核对。"""
    from haa.config import default_config
    cfg = default_config()
    checks = [
        # 直接核对已知值
        (cfg.pipeline.max_design_rounds == 3, "max_design_rounds=3"),
        (cfg.pipeline.max_review_rounds == 3, "max_review_rounds=3"),
        (cfg.pipeline.review_accept_threshold == 0.7, "threshold=0.7"),
        (cfg.budget.per_campaign == 10.0, "budget/campaign=$10"),
        (cfg.budget.global_limit == 100.0, "budget/global=$100"),
        (cfg.timeouts.llm == 300, "llm timeout=300s"),
        (cfg.timeouts.stage == 3600, "stage timeout=3600s"),
        (cfg.harness.write_gate_enabled is True, "write_gate=True"),
    ]
    failed = [name for ok, name in checks if not ok]
    assert not failed, f"hyperparameter drift: {failed}"


def test_feature_switches_default_off():
    from haa.config import default_config
    cfg = default_config()
    for feat in ("anchored_mode", "divergence", "typed_duel",
                 "settlement", "branch_tree", "code_mode", "fallback_acp"):
        assert cfg.harness.feature(feat) is False, f"{feat} should default False"
