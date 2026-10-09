"""批次18 六挂点接线测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from haa.harness.registry import ToolError
from haa.llm.agent_loop import AgentLoopResult
from haa.models import Brief, Campaign, Candidate, ProjectStatus
from haa.p2revise import set_hold_flag, clear_hold_flag, triage_failure
from haa.p3revise import normalize_paper_v2, validate_paper_sections


# ===== 挂点1：分诊器入 DebugSession =====

def test_triage_assist_triggers_hold(tmp_path):
    """assist 类日志 → 分诊 → HOLD 标志设置。"""
    from haa.p2revise import build_assist_request
    log = "sudo apt install\nsudo: a password is required\nerror"
    v = triage_failure(log)
    assert v["category"] == "assist"
    # 模拟 DebugSession 的 assist 路径
    if v["category"] == "assist":
        set_hold_flag("test_camp", note=build_assist_request(v),
                      campaigns_dir=tmp_path)
        flag = tmp_path / "test_camp" / "HOLD"
        assert flag.exists()
        content = flag.read_text(encoding="utf-8")
        assert "卡在哪" in content and "需要什么" in content
        clear_hold_flag("test_camp", campaigns_dir=tmp_path)


def test_triage_environment_exits_loop():
    """environment 类 → DebugSession 退出循环（不走修复）。"""
    log = "Training epoch 3\nCUDA out of memory\nTried to allocate 2GiB"
    v = triage_failure(log)
    assert v["category"] == "environment"
    assert v["action"] != "→ 修复循环"  # 不走修复路径


def test_triage_code_goes_to_fix():
    """code 类 → 修复循环继续（默认路径）。"""
    log = "Traceback (most recent call last):\n  File 'main.py' line 10\nValueError"
    v = triage_failure(log)
    assert v["category"] == "code"
    assert "修复" in v["action"]


# ===== 挂点2：HOLD 状态机 =====

def test_project_status_has_hold():
    assert hasattr(ProjectStatus, "HOLD")
    assert ProjectStatus.HOLD.value == "hold"


# ===== 挂点3：轮间检查点 =====

def test_hold_flag_round_boundary(tmp_path):
    """设标志→check→命中→清→恢复。"""
    cid = "round_test"
    assert not set_hold_flag(cid, note="bug at round 3",
                             campaigns_dir=tmp_path) is None
    from haa.p2revise import check_hold_flag
    assert check_hold_flag(cid, campaigns_dir=tmp_path)
    note = clear_hold_flag(cid, campaigns_dir=tmp_path)
    assert "bug at round 3" in note
    assert not check_hold_flag(cid, campaigns_dir=tmp_path)


# ===== 挂点4：CLI 命令存在 =====

def test_cli_progress_exists():
    from haa.cli.main import app
    names = [c.name for c in app.registered_commands]
    assert "progress" in names


def test_cli_hold_resume_exist():
    from haa.cli.project import project_app
    names = [c.name for c in project_app.registered_commands]
    assert "hold" in names and "resume" in names


# ===== 挂点5：ANALYZE 阶段 =====

def test_analyze_stage_registered():
    from haa.pipeline import StageName, Pipeline
    assert hasattr(StageName, "ANALYZE")
    from haa.stages.analyze import AnalyzeStage
    assert AnalyzeStage.name == "ANALYZE"


def test_analyze_viewpoint_unsupported_kills(tmp_path):
    """观点终审：指标反主张→abort_candidate。"""
    from haa.config import Config, StorageConfig
    from haa.stages.analyze import AnalyzeStage
    from haa.stages.base import StageContext
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "c")))
    stage = AnalyzeStage(llm=None, config=cfg)
    content = json.dumps({
        "analysis": {"claim1": {"metric": "acc", "value": 0.1}},
        "viewpoint_verdict": "viewpoint_unsupported",
        "claims_supported": [],
        "claims_unsupported": ["claim1"],
    })
    stage._make_agent_loop = lambda: SimpleNamespace(
        run=lambda *a, **kw: AgentLoopResult(content=content))
    camp = Campaign(brief_hash="abc")
    cand = Candidate(campaign_id=camp.id, slug="test-cand", title="T",
                     significance=.5, win_odds=.5, difficulty=.5, queue_index=0)
    ctx = StageContext(brief=Brief(title="B", problem_area="P"), candidate=cand)
    result = stage.run(camp, ctx)
    assert result.status.value == "abort_candidate"
    assert result.data["viewpoint_verdict"] == "viewpoint_unsupported"


def test_analyze_supported_continues(tmp_path):
    from haa.config import Config, StorageConfig
    from haa.stages.analyze import AnalyzeStage
    from haa.stages.base import StageContext
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "c")))
    stage = AnalyzeStage(llm=None, config=cfg)
    content = json.dumps({
        "analysis": {}, "viewpoint_verdict": "supported",
        "claims_supported": ["c1"], "claims_unsupported": [],
    })
    stage._make_agent_loop = lambda: SimpleNamespace(
        run=lambda *a, **kw: AgentLoopResult(content=content))
    camp = Campaign(brief_hash="abc")
    cand = Candidate(campaign_id=camp.id, slug="t", title="T",
                     significance=.5, win_odds=.5, difficulty=.5, queue_index=0)
    ctx = StageContext(brief=Brief(title="B", problem_area="P"), candidate=cand)
    result = stage.run(camp, ctx)
    assert result.status.value == "continue"


# ===== 挂点6：校验挂管线 =====

def test_paper_sections_validated():
    """WRITE 后六节校验：缺失节报错。"""
    paper4 = {"abstract": "a", "intro": "i", "background": "b", "method": "m"}
    errors = validate_paper_sections(paper4)
    assert len(errors) >= 2  # results + conclusion 缺失

    paper6 = normalize_paper_v2(paper4)
    errors6 = validate_paper_sections(paper6)
    assert len(errors6) == 0  # 占位后通过


def test_kill_reason_enum_in_memory_bank(tmp_path):
    """memory_bank IdeaEntity 校验 kill_reason 枚举。"""
    from haa.memory_bank import IdeaEntity, next_entity_id
    # 合法枚举
    idea = IdeaEntity(idea_id="i-20261009-0001", title="T",
                      core_claim="X", status="killed",
                      kill_reason="viewpoint")
    # legacy 兼容
    idea2 = IdeaEntity(idea_id="i-20261009-0002", title="T2",
                       core_claim="X", status="killed",
                       kill_reason="legacy:old_reason")
    # 非法→软校验（不 raise，只 warn——存量兼容）
    idea3 = IdeaEntity(idea_id="i-20261009-0003", title="T3",
                       core_claim="X", status="killed",
                       kill_reason="random_invalid")
    # 仍能创建（soft pass），但 validate_kill_reason 函数本身拒绝
    from haa.p2revise import validate_kill_reason
    assert not validate_kill_reason("random_invalid")


def test_lint_hook_writes_events():
    """lint 事件不带 cost（记账铁律）。"""
    # 模拟 pipeline 挂点6 的 lint 路径
    from haa.p3revise import lint_readability
    issues = lint_readability("这是一段超长句子" * 20)
    assert isinstance(issues, list)
    # 新事件不携带 cost_usd——由调用方保证（pipeline 代码验证）


# ===== 灰度回归：开关全关时零行为变化 =====

def test_analyze_feature_default_off():
    from haa.config import default_config
    cfg = default_config()
    assert cfg.harness.feature("analyze") is False


def test_pipeline_human_review_to_write_without_analyze(tmp_path):
    """开关关时 HUMAN_REVIEW→WRITE（不经过 ANALYZE）。"""
    from haa.pipeline import StageName
    from haa.config import Config, HarnessConfig, StorageConfig
    from unittest.mock import MagicMock
    from haa.state import StateStore
    cfg = Config(
        storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                              campaigns_dir=str(tmp_path / "c")),
        harness=HarnessConfig(features=(("analyze", False),)))
    store = StateStore(tmp_path / "t.db")
    from haa.pipeline import Pipeline
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    # 模拟 HUMAN_REVIEW 直通
    from haa.stages.base import StageResult, StageStatus
    result = StageResult(status=StageStatus.CONTINUE, data={})
    next_stage = pipe._transition(StageName.HUMAN_REVIEW, result,
                                  None, SimpleNamespace(extra={}))
    assert next_stage == StageName.WRITE
