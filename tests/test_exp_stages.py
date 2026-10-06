"""Phase A 实验 stage 测试。

验证 EXP_SPEC⇄EXP_FEASIBILITY 双层循环的全部转移分支、checkpoint 序列化、
预算门控、GRADE 后继改为 EXP_SPEC、EXP_FEASIBILITY 只读权限。
"""

from __future__ import annotations

import pytest

from haa.budget import BudgetManager, is_gated_stage
from haa.config import default_config
from haa.models import Brief, Candidate
from haa.pipeline import CampaignContext, Pipeline, StageName
from haa.stages.base import StageContext, StageResult, StageStatus
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture
def brief():
    return Brief(title="T", problem_area="P")


@pytest.fixture
def cfg():
    return default_config()


def _pipe(store, cfg):
    return Pipeline(cfg, store, BudgetManager(store, global_limit=100), llm=object(), stages={})


def _ctx(store, brief, exp_round=0, exp_to_design=0):
    camp = store.create_campaign(brief)
    ctx = CampaignContext(brief=brief)
    ctx.candidate = Candidate(
        campaign_id=camp.id, slug="c", title="C",
        significance=0.5, win_odds=0.5, difficulty=0.5, queue_index=0,
    )
    ctx.exp_round = exp_round
    ctx.exp_to_design_round = exp_to_design
    ctx.extra["exp_spec"] = {"experiments": []}
    return camp, ctx


def _blockers(*items):
    """items: (severity, category, detail, fix_suggestion)."""
    return [
        {"severity": s, "category": c, "detail": d, "evidence": "", "fix_suggestion": f}
        for s, c, d, f in items
    ]


def _result(blockers):
    return StageResult(data={"blockers": blockers, "assessment": ""})


# --- 1. PASS → WRITE --------------------------------------------------------

def test_exp_pass_to_write(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief)
    assert pipe._after_exp_feasibility(_result([]), camp, ctx) == StageName.HUMAN_REVIEW


# --- 2. major, 内循环未满 → EXP_SPEC, round+1 --------------------------------

def test_exp_major_rework(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_round=0)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("major", "baseline", "weak", "fix"))), camp, ctx,
    )
    assert nxt == StageName.EXP_SPEC
    assert ctx.exp_round == 1


# --- 3. major, 内循环耗尽 → WRITE degraded -----------------------------------

def test_exp_major_cap_to_write(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_round=cfg.pipeline.max_exp_rounds)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("major", "baseline", "weak", "fix"))), camp, ctx,
    )
    assert nxt == StageName.HUMAN_REVIEW
    assert ctx.extra.get("exp_degraded") is True


# --- 4. fatal 方案层 + 大循环可用 → DESIGN -----------------------------------

def test_exp_fatal_design_level_to_design(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_to_design=0)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("fatal", "baseline", "bad", "try alternative approach"))), camp, ctx,
    )
    assert nxt == StageName.DESIGN
    assert ctx.exp_to_design_round == 1
    assert ctx.design_round == 0  # 重置
    # 不可行原因注入 verify_findings（供 DESIGN 看见）
    assert any(f.get("kind") == "exp_infeasible" for f in ctx.verify_findings)


# --- 5. fatal data + 大循环耗尽 → WRITE theory_only --------------------------

def test_exp_fatal_data_theory_only(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_to_design=cfg.pipeline.max_exp_to_design_rounds)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("fatal", "data", "dataset gone", ""))), camp, ctx,
    )
    assert nxt == StageName.HUMAN_REVIEW
    assert ctx.extra.get("theory_only") is True


# --- 6. fatal metric 方案层 → DESIGN（同 4 的 category 变体）------------------

def test_exp_fatal_metric_design(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_to_design=0)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("fatal", "metric", "bad", "reformulate metric"))), camp, ctx,
    )
    assert nxt == StageName.DESIGN


# --- 7. fatal 非方案层/非data + 循环耗尽 → 杀候选（advance_queue→retire）----

def test_exp_fatal_abort_candidate(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief, exp_to_design=cfg.pipeline.max_exp_to_design_rounds)
    nxt = pipe._after_exp_feasibility(
        _result(_blockers(("fatal", "literature", "far off standard", ""))), camp, ctx,
    )
    # 无下一个候选 → retire → None
    assert nxt is None


# --- 8. 候选重置清零 exp 计数 ------------------------------------------------

def test_reset_clears_exp_rounds(store, brief, cfg):
    pipe = _pipe(store, cfg)
    _, ctx = _ctx(store, brief, exp_round=2, exp_to_design=1)
    pipe._reset_candidate_context(ctx)
    assert ctx.exp_round == 0
    assert ctx.exp_to_design_round == 0


# --- 9. checkpoint 序列化含 exp_round / exp_to_design_round ------------------

def test_checkpoint_has_exp_rounds(brief):
    ctx = CampaignContext(brief=brief)
    ctx.exp_round = 2
    ctx.exp_to_design_round = 1
    d = ctx.to_checkpoint_dict()
    assert d["exp_round"] == 2
    assert d["exp_to_design_round"] == 1
    ctx2 = CampaignContext.from_checkpoint(d)
    assert ctx2.exp_round == 2
    assert ctx2.exp_to_design_round == 1


# --- 10. EXP_SPEC / EXP_FEASIBILITY 是 gated stages --------------------------

def test_exp_stages_gated():
    assert is_gated_stage("EXP_SPEC") is True
    assert is_gated_stage("EXP_FEASIBILITY") is True


# --- 11. GRADE SOLID/THIN → EXP_SPEC（不再直接到 WRITE）----------------------

def test_grade_to_exp_spec(store, brief, cfg):
    pipe = _pipe(store, cfg)
    camp, ctx = _ctx(store, brief)
    result = StageResult(status=StageStatus.CONTINUE, data={"grade": "solid"})
    assert pipe._after_grade(result, camp, ctx) == StageName.EXP_SPEC


# --- 12. EXP_FEASIBILITY 无 write_file/edit_file 权限 ------------------------

def test_exp_feasibility_no_write_tools():
    from haa.stages import ExpFeasibilityStage
    s = ExpFeasibilityStage()
    assert "write_file" not in s.allowed_tools
    assert "edit_file" not in s.allowed_tools
    # EXP_SPEC 可以 write_file
    from haa.stages import ExpSpecStage
    assert "write_file" in ExpSpecStage().allowed_tools
