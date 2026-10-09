"""批次18-1 返工测试——真实路径驱动（mock 仅限 transport/LLM 边界）。

§5 测试特别条款：
1. 禁止测试内重演接线逻辑——必须驱动真实代码路径
2. 事件测试测事件——断言事件真实落库
3. 回归测试测开关——双开关各一条"关=零行为变化"
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from haa.config import Config, HarnessConfig, StorageConfig
from haa.harness.registry import ToolRegistry
from haa.harness.tools.native import apply_native_tools
from haa.llm.agent_loop import AgentLoopResult
from haa.models import Brief, Campaign, Candidate, ProjectStatus
from haa.stages.base import StageContext, StageResult, StageStatus


def _cfg(tmp_path, **features):
    feat = {("analyze", features.get("analyze", False)),
            ("p2_triage", features.get("p2_triage", False)),
            ("pilot", features.get("pilot", False))}
    return Config(
        storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                              campaigns_dir=str(tmp_path / "camps")),
        harness=HarnessConfig(features=tuple(feat)),
    )


# ========== R1: HOLD 三出口分流（真实驱动 _run_batch → _p2_execute） ==========

def _drive_run_p2_batch(tmp_path, debug_reason: str):
    """D3 批次18-2：真驱动 _run_p2_batch（mock 仅限 coding agent/execute
    进程边界）→ 失败分流路由是真实代码路径。"""
    from unittest.mock import patch

    from haa.models import Precursor, Project
    from haa.project_controller import ProjectController
    from haa.state import StateStore

    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    precursor = Precursor(campaign_id="cx", candidate_id="cd",
                          candidate_slug="slug", candidate_title="T")
    proj = Project(id=f"proj_r1_{abs(hash(debug_reason)) % 10000}", brief=brief,
                   precursors=[precursor],
                   selected_precursor_campaign_id="cx")
    store.save_project(proj)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())
    ctrl.llm.call.return_value.content = "(diagnostic)"
    fake_debug = SimpleNamespace(
        success=False, phase="phase_a", rounds_a=2, rounds_b=0,
        reason=debug_reason, error="boom", log="log tail",
        metrics={}, results_dir=None)
    with patch.object(ctrl, "_build_coding_agent", return_value=MagicMock()), \
         patch.object(ctrl, "_p2_code_gen", return_value=tmp_path / "code"), \
         patch.object(ctrl, "_p2_execute", return_value=fake_debug):
        out = ctrl._run_p2_batch(proj)
    return out, store


def _make_project(store, brief):
    from haa.models import Project, Phase
    from haa.models import Brief as B
    proj = Project(id="test_proj_001", brief=B(title=brief.title, problem_area=brief.problem_area))
    store.save_project(proj)
    return proj


def test_r1_assist_hold_sets_project_hold_not_moribund(tmp_path):
    """assist 类失败→ProjectStatus.HOLD（非 MORIBUND）+ hold_entered 事件。"""
    from haa.state import StateStore
    from haa.project_controller import ProjectController
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    proj = _make_project(store, brief)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())
    fake_debug = SimpleNamespace(
        success=False, phase="phase_a", rounds_a=3, reason="assist_hold",
        error="sudo: a password is required", log="sudo install\nsudo: a password is required",
        metrics={}, results_dir=None)
    result = ctrl._set_hold_p2(proj, fake_debug, None, "assist_hold")
    assert result.status == ProjectStatus.HOLD
    assert result.hold_reason == "assist_hold"
    events = store.list_events(proj.id)
    hold_events = [e for e in events if e.event_type == "hold_entered"]
    assert len(hold_events) >= 1
    assert hold_events[0].payload.get("reason") == "assist_hold"
    assist_events = [e for e in events if e.event_type == "assist_request"]
    assert len(assist_events) >= 1
    assert (hold_events[0].cost_usd or 0) == 0  # 不带 cost


def test_r1_environment_sets_hold_not_moribund(tmp_path):
    from haa.state import StateStore
    from haa.project_controller import ProjectController
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    proj = _make_project(store, brief)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())
    fake_debug = SimpleNamespace(
        success=False, phase="phase_a", rounds_a=2,
        reason="environment_issue", error="CUDA out of memory",
        log="CUDA out of memory", metrics={}, results_dir=None)
    result = ctrl._set_hold_p2(proj, fake_debug, None, "environment_issue")
    assert result.status == ProjectStatus.HOLD
    events = store.list_events(proj.id)
    triage = [e for e in events if e.event_type == "triage_verdict"]
    assert len(triage) >= 1
    assert triage[0].payload.get("category") == "environment"


def test_r1_routing_assist_hold_via_run_p2_batch(tmp_path):
    """D3 三态路由①：reason=assist_hold → 真实 _run_p2_batch 路由进 HOLD。"""
    out, store = _drive_run_p2_batch(tmp_path, "assist_hold")
    assert out.status == ProjectStatus.HOLD
    assert out.hold_reason == "assist_hold"
    assert out.status != ProjectStatus.MORIBUND
    assert any(e.event_type == "hold_entered"
               for e in store.list_events(out.id))


def test_r1_routing_hold_detected_via_run_p2_batch(tmp_path):
    """D3 三态路由②：reason=hold_detected → HOLD（等人）。"""
    out, _ = _drive_run_p2_batch(tmp_path, "hold_detected")
    assert out.status == ProjectStatus.HOLD
    assert out.hold_reason == "hold_detected"
    assert out.status != ProjectStatus.MORIBUND


def test_r1_routing_environment_issue_via_run_p2_batch(tmp_path):
    """D3 三态路由③：reason=environment_issue → HOLD（环境重配）。"""
    out, _ = _drive_run_p2_batch(tmp_path, "environment_issue")
    assert out.status == ProjectStatus.HOLD
    assert out.hold_reason == "environment_issue"
    assert out.status != ProjectStatus.MORIBUND


def test_r1_other_failure_goes_to_moribund(tmp_path):
    """D3 整改：真驱动 reason 路由（_run_p2_batch + 普通失败）→ 进
    _set_moribund_p2、状态 MORIBUND——不再只断言枚举不等。"""
    out, store = _drive_run_p2_batch(tmp_path, "phase_a_circuit_breaker")
    assert out.status == ProjectStatus.MORIBUND
    assert out.status != ProjectStatus.HOLD
    assert out.moribund_reason.startswith("cap:")  # C1 枚举映射
    assert out.moribund_history                   # 诊断史入档
    assert not [e for e in store.list_events(out.id)
                if e.event_type == "hold_entered"]


# ========== R2: ANALYZE 恢复入口分流 ==========

def test_r2_analyze_on_resume_goes_to_analyze(tmp_path):
    """D3 整改：features.analyze=true → 真驱动 _resume 断言恢复入 ANALYZE。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    from haa.models import CampaignStatus
    cfg = _cfg(tmp_path, analyze=True)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    camp.status = CampaignStatus.AWAITING_HUMAN_REVIEW
    store.save_campaign(camp)
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    snap = store.restore(camp.id)
    context, stage = pipe._resume(camp, snap, brief)
    assert stage == StageName.ANALYZE  # 生产入口分流：analyze 开 → ANALYZE


def test_r2_analyze_off_resume_goes_to_write(tmp_path):
    """D3 整改：features.analyze=false → 真驱动 _resume 断言恢复入 WRITE。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    from haa.models import CampaignStatus
    cfg = _cfg(tmp_path, analyze=False)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    camp.status = CampaignStatus.AWAITING_HUMAN_REVIEW
    store.save_campaign(camp)
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    snap = store.restore(camp.id)
    context, stage = pipe._resume(camp, snap, brief)
    assert stage == StageName.WRITE  # 零行为回归：关 → WRITE


# ========== R3: ANALYZE/PILOT abort_candidate → kill ==========

def test_r3_analyze_abort_kills_candidate(tmp_path):
    """ANALYZE viewpoint_unsupported → _record_kill + advance。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    # 设上下文
    from haa.stages.base import StageContext
    from haa.models import Candidate
    cand = Candidate(campaign_id=camp.id, slug="test-slug", title="T",
                     significance=.5, win_odds=.5, difficulty=.5, queue_index=0)
    context = StageContext(brief=brief, candidate=cand)
    result = StageResult(status=StageStatus.ABORT_CANDIDATE,
                         data={"reason": "viewpoint", "analysis": {}})
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    # 驱动 _transition(ANALYZE, abort_result)
    next_stage = pipe._transition(StageName.ANALYZE, result, camp, context)
    # kills 落库
    kills = context.extra.get("kills", [])
    assert len(kills) >= 1
    assert kills[0]["stage"] == "ANALYZE"
    assert "viewpoint" in kills[0]["reason"]


def test_r3_pilot_abort_kills_candidate(tmp_path):
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    cand = Candidate(campaign_id=camp.id, slug="p-slug", title="T",
                     significance=.5, win_odds=.5, difficulty=.5, queue_index=0)
    context = StageContext(brief=brief, candidate=cand)
    result = StageResult(status=StageStatus.ABORT_CANDIDATE,
                         data={"reason": "pilot: not_supported"})
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    pipe._transition(StageName.PILOT, result, camp, context)
    kills = context.extra.get("kills", [])
    assert len(kills) >= 1 and kills[0]["stage"] == "PILOT"


# ========== R6: WRITE/REFINE 校验（paper 回写后+事件） ==========

def test_r6_write_triggers_validation_event(tmp_path):
    """WRITE 后校验→paper_section_check 事件落库。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    # 四节 paper（缺 results/conclusion → 校验有错误）
    paper4 = {"abstract": "a", "intro": "i", "background": "b", "method": "m",
              "title": "Test"}
    result = StageResult(status=StageStatus.CONTINUE, data=paper4)
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    pipe._transition(StageName.WRITE, result, camp, context)
    # paper 已回写并规范化
    assert "results" in context.paper  # normalize 填了占位
    # 事件落库
    events = store.list_events(camp.id)
    check_events = [e for e in events if e.event_type == "paper_section_check"]
    assert len(check_events) >= 1
    assert check_events[0].payload.get("errors") is not None


def test_r6_refine_validates_new_paper(tmp_path):
    """REFINE 后校验对象为新稿（非旧稿）。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    # 先设旧稿
    context.paper = {"abstract": "old", "method": "old", "title": "T"}
    # REFINE 产新稿
    new_paper = {"abstract": "NEW_v2", "intro": "new", "background": "new",
                 "method": "new_method", "title": "T"}
    result = StageResult(status=StageStatus.CONTINUE, data=new_paper)
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    pipe._transition(StageName.REFINE, result, camp, context)
    # 校验的是新稿
    assert context.paper["abstract"] == "NEW_v2"  # 新稿已回写
    assert "results" in context.paper  # 新稿被 normalize


# ========== R5: 五类新事件真实落库 ==========

def test_r5_analyze_done_event(tmp_path):
    """ANALYZE 完成→analyze_done 事件。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    result = StageResult(status=StageStatus.CONTINUE,
                         data={"viewpoint_verdict": "supported",
                               "claims_supported": ["c1"],
                               "claims_unsupported": [],
                               "analysis": {}})
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    pipe._transition(StageName.ANALYZE, result, camp, context)
    events = store.list_events(camp.id)
    done = [e for e in events if e.event_type == "analyze_done"]
    assert len(done) >= 1
    assert done[0].payload.get("verdict") == "supported"
    assert (done[0].cost_usd or 0) == 0  # 不带 cost（默认 0.0 非注入）


# ========== R9: p2_triage 开关（D3 整改：注册表断言，非空断言） ==========

def test_r9_triage_feature_default_off():
    """C2 注册后：default.yaml 注册表含 p2_triage 且默认 false。"""
    from haa.config import load_config

    cfg = load_config()  # 无 HAA_CONFIG 时取 config/default.yaml
    assert ("p2_triage", False) in cfg.harness.features
    assert cfg.harness.feature("p2_triage") is False


# ========== R10: calculator 退役 ==========

def test_r10_analyze_no_calculator():
    from haa.stages.analyze import AnalyzeStage
    assert "calculator" not in AnalyzeStage.allowed_tools


# ========== 灰度回归：开关全关零行为 ==========

def test_regression_all_off_zero_change(tmp_path):
    """全部开关关闭时：HUMAN_REVIEW→WRITE（不经 ANALYZE）。"""
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path)  # all defaults=False
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    result = StageResult(status=StageStatus.CONTINUE, data={})
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    nxt = pipe._transition(StageName.HUMAN_REVIEW, result, camp, context)
    assert nxt == StageName.WRITE  # 直通 WRITE


def test_regression_analyze_on_goes_through_analyze(tmp_path):
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = _cfg(tmp_path, analyze=True)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    result = StageResult(status=StageStatus.CONTINUE, data={})
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    nxt = pipe._transition(StageName.HUMAN_REVIEW, result, camp, context)
    assert nxt == StageName.ANALYZE
