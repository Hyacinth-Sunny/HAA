"""批次18-2 返工测试——真实路径驱动（mock 仅限 transport/LLM/store 边界）。

§0 纪律：测试驱动真实代码路径；事件测试断言事件真实落库；每组测试与
返工单 A/B/C/D 四组一一对应。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from haa.config import Config, HarnessConfig, StorageConfig
from haa.llm.agent_loop import AgentLoopResult
from haa.models import Brief, CampaignStatus, ProjectStatus
from haa.stages.base import StageContext


def _cfg(tmp_path, **features):
    feat = {("analyze", features.get("analyze", False)),
            ("p2_triage", features.get("p2_triage", False)),
            ("pilot", features.get("pilot", False))}
    return Config(
        storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                              campaigns_dir=str(tmp_path / "camps")),
        harness=HarnessConfig(features=tuple(feat)),
    )


def _pipeline(tmp_path, **features):
    from haa.pipeline import Pipeline, StageName

    cfg = _cfg(tmp_path, **features)
    from haa.state import StateStore

    store = StateStore(tmp_path / "t.db")
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    return pipe, store


def _isolate_campaigns_dir(monkeypatch, tmp_path) -> Path:
    """把 p2revise 的旗标默认目录隔离到 tmp（防测试触碰真实 data/campaigns）。"""
    monkeypatch.setattr("haa.config._PROJECT_ROOT", Path(tmp_path))
    camps = Path(tmp_path) / "data" / "campaigns"
    camps.mkdir(parents=True, exist_ok=True)
    return camps


# ========== A1: exp_results → _resume 桥接（生产者+消费者） ==========

def test_a1_resume_bridges_exp_results(tmp_path):
    """project.exp_results → context.extra[exp_metrics/exp_log 尾部]。"""
    from haa.models import Project
    from haa.pipeline import StageName

    pipe, store = _pipeline(tmp_path)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    camp.status = CampaignStatus.AWAITING_HUMAN_REVIEW
    store.save_campaign(camp)
    proj = Project(id="proj_a1", brief=brief,
                   selected_precursor_campaign_id=camp.id)
    proj.exp_results = {"metrics": {"acc": 0.85, "loss": 0.12},
                        "log": "x" * 3000 + "TRAIN_DONE",
                        "analysis": {"c1": {"verdict": "supported"}}}
    store.save_project(proj)

    snap = store.restore(camp.id)
    context, stage = pipe._resume(camp, snap, brief)
    assert context.extra["exp_metrics"] == {"acc": 0.85, "loss": 0.12}
    assert context.extra["exp_log"].endswith("TRAIN_DONE")
    assert len(context.extra["exp_log"]) <= 2000  # 尾部截断
    assert stage == StageName.WRITE  # analyze 关 → 恢复入 WRITE


def test_a1_resume_no_association_degrades(tmp_path):
    """无关联 project → 降级为空（不抛、不填键）。"""
    pipe, store = _pipeline(tmp_path)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    snap = store.restore(camp.id)
    context, stage = pipe._resume(camp, snap, brief)
    assert "exp_metrics" not in context.extra
    assert "exp_log" not in context.extra


def test_a1_resume_via_ids_list(tmp_path):
    """多选清单 selected_precursor_campaign_ids 也能命中关联。"""
    from haa.models import Project

    pipe, store = _pipeline(tmp_path)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    proj = Project(id="proj_a2", brief=brief,
                   selected_precursor_campaign_ids=[camp.id])
    proj.exp_results = {"metrics": {"f1": 0.7}}
    store.save_project(proj)
    snap = store.restore(camp.id)
    context, _ = pipe._resume(camp, snap, brief)
    assert context.extra["exp_metrics"] == {"f1": 0.7}


# ========== A2: WRITE 消费 analysis（提示词 + 六节捕获） ==========

def test_a2_analyze_prompt_captures_metrics(tmp_path):
    """ANALYZE 开 → _resume 桥接后 ANALYZE 提示词捕获 metrics（非空）。"""
    from haa.models import Project
    from haa.pipeline import StageName
    from haa.stages.analyze import AnalyzeStage

    pipe, store = _pipeline(tmp_path, analyze=True)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    camp.status = CampaignStatus.AWAITING_HUMAN_REVIEW
    store.save_campaign(camp)
    proj = Project(id="proj_a3", brief=brief,
                   selected_precursor_campaign_id=camp.id)
    proj.exp_results = {"metrics": {"acc": 0.85}, "log": "TRAIN_DONE"}
    store.save_project(proj)

    snap = store.restore(camp.id)
    context, stage = pipe._resume(camp, snap, brief)
    assert stage == StageName.ANALYZE

    from haa.models import Candidate
    context.candidate = Candidate(campaign_id=camp.id, slug="s", title="T",
                                  significance=.5, win_odds=.5, difficulty=.5,
                                  queue_index=0)
    stage_obj = AnalyzeStage()
    captured = {}

    def fake_run_agent(prompt, **kw):
        captured["prompt"] = prompt
        return AgentLoopResult(content=json.dumps(
            {"analysis": {"c1": {"metric": "acc", "verdict": "supported"}},
             "viewpoint_verdict": "supported"}))

    stage_obj._run_agent = fake_run_agent
    res = stage_obj.run(camp, context)
    assert "0.85" in captured["prompt"]  # 指标真实进了提示词
    assert res.data["viewpoint_verdict"] == "supported"


def test_a2_write_prompt_analysis_block():
    """WRITE 提示词：有 analysis → 取材块+工件内容；无 → no-experiment 规则。"""
    from haa.prompts import render_prompt

    p = render_prompt("write", candidate=None, design=None,
                      verify_passed=True,
                      extra={"analysis": {"claim_1": {"metric": "acc",
                                                      "value": 0.85,
                                                      "verdict": "supported"}}},
                      brief=None)
    assert "实验分析工件" in p
    assert "claim_1" in p and "0.85" in p
    assert "## 4 Results" in p  # 写实指令在位

    p2 = render_prompt("write", candidate=None, design=None,
                       verify_passed=True, extra={}, brief=None)
    assert "no-experiment" in p2
    assert "不得编造" in p2


def test_a2_write_stage_captures_six_sections(tmp_path):
    """WriteStage 捕获 results/conclusion（六节）——不再只收四节丢弃。"""
    from haa.stages.write import WriteStage

    ws = WriteStage()

    def fake_run_agent(prompt, **kw):
        return AgentLoopResult(content=json.dumps({
            "title": "T", "abstract": "a", "intro": "i",
            "background": "b", "method": "m",
            "results": "## 4 Results\nacc=0.85（依据 claim_1）",
            "conclusion": "## 5 Conclusion\n主张成立",
            "outline": [], "self_negation_scan": [],
        }))

    ws._run_agent = fake_run_agent
    camp = SimpleNamespace(id="camp_w")
    context = StageContext(brief=Brief(title="T", problem_area="P"))
    res = ws.run(camp, context)
    assert res.data["results"].startswith("## 4 Results")
    assert res.data["conclusion"].startswith("## 5 Conclusion")


# ========== A3: analysis.json 落盘 ==========

def test_a3_analyze_artifact_written(tmp_path):
    """ANALYZE:done + extra.analysis → artifacts/<slug>/analysis.json。"""
    from haa.artifacts import write_artifacts

    ctx = SimpleNamespace(
        candidate=SimpleNamespace(slug="cand-a"),
        extra={"analysis": {"claim_1": {"verdict": "supported"}}})
    write_artifacts(tmp_path, "camp1", ctx, "ANALYZE:done")
    f = tmp_path / "camp1" / "artifacts" / "cand-a" / "analysis.json"
    assert f.exists()
    assert "claim_1" in json.loads(f.read_text(encoding="utf-8"))

    # 无 analysis → None 跳过，不落盘
    ctx2 = SimpleNamespace(candidate=SimpleNamespace(slug="cand-b"), extra={})
    write_artifacts(tmp_path, "camp1", ctx2, "ANALYZE:done")
    assert not (tmp_path / "camp1" / "artifacts" / "cand-b" /
                "analysis.json").exists()


# ========== A4: 每轮分诊事件 triage_verdict ==========

class _CrashTransport:
    """伪执行后端：每轮必崩（边界 mock——真实驱动 DebugSession 循环）。"""

    def deploy(self, code_dir, run_id):
        return Path(code_dir)

    def run(self, exec_dir, command, timeout=None):
        from haa.p2.transport import RunResult
        return RunResult(exit_code=1, stdout="",
                         stderr="Traceback (most recent call last):\n"
                                "ValueError: bad shape")

    def download_results(self, exec_dir, results_dir):
        pass


def test_a4_triage_event_per_round(tmp_path):
    """两轮崩溃 → 两条 triage_verdict 事件（payload=category+evidence）。"""
    from haa.p2.debug_session import DebugConfig, DebugSession

    events: list[tuple[str, dict]] = []
    session = DebugSession(
        transport=_CrashTransport(),
        coding_agent=MagicMock(),
        config=DebugConfig(max_hard_error_rounds=2),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c1",
        event_sink=lambda etype, payload: events.append((etype, payload)),
    )
    result = session.run()
    assert not result.success  # 2 轮崩 → 断路器
    triage_events = [e for e in events if e[0] == "triage_verdict"]
    assert len(triage_events) == 2
    etype, payload = triage_events[0]
    assert payload["category"] == "code"
    assert isinstance(payload["evidence_lines"], list)
    assert payload["round"] in (1, 2)


def test_a4_event_sink_failure_does_not_break_loop(tmp_path):
    """event_sink 抛异常 → 调试循环不受影响（可观测层不阻断）。"""
    from haa.p2.debug_session import DebugConfig, DebugSession

    def bad_sink(etype, payload):
        raise RuntimeError("sink down")

    session = DebugSession(
        transport=_CrashTransport(),
        coding_agent=MagicMock(),
        config=DebugConfig(max_hard_error_rounds=1),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c1",
        event_sink=bad_sink,
    )
    result = session.run()
    assert not result.success
    assert result.reason == "phase_a_circuit_breaker"  # 循环走到断路器


def test_a4_controller_wires_sink_into_session(tmp_path, monkeypatch):
    """project_controller._p2_execute 注入 event_sink（真实接线）。"""
    from haa.project_controller import ProjectController
    from haa.state import StateStore

    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    from haa.models import Project
    proj = Project(id="proj_sink", brief=brief)
    store.save_project(proj)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())

    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.got = kwargs

        def run(self):
            assert captured["event_sink"] is not None
            captured["event_sink"]("triage_verdict", {"category": "code"})
            from haa.p2.debug_session import DebugResult
            return DebugResult(success=False, phase="phase_a", rounds_a=1,
                               reason="phase_a_circuit_breaker", error="x")

    import haa.p2.debug_session as ds
    monkeypatch.setattr(ds, "DebugSession", _FakeSession)
    monkeypatch.setattr(ctrl, "_build_p2_transport", lambda: MagicMock())
    ctrl._p2_execute(proj, tmp_path / "code", MagicMock(), tmp_path)
    events = store.list_events(proj.id)
    assert any(e.event_type == "triage_verdict" for e in events)


# ========== A5: hold_resumed 事件（CLI resume 成功时） ==========

def test_a5_hold_resumed_event(tmp_path, monkeypatch):
    """resume 清掉 HOLD 旗标 → hold_resumed 事件落库（无 cost）。"""
    from typer.testing import CliRunner

    from haa.models import Project
    from haa.state import StateStore

    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    store = StateStore(tmp_path / "t.db")
    # 注意：project_resume 用 main.py 的模块级 StateStore 绑定（非函数内
    # import），补丁必须打在 haa.cli.main 命名空间——打 haa.state 只对
    # call-time import 生效（B1 改走 _store() 后目标随之调整）。
    monkeypatch.setattr("haa.cli.main.StateStore",
                        lambda *a, **kw: store)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    proj = Project(id="proj_a5", brief=brief)
    store.save_project(proj)

    from haa.p2revise import set_hold_flag
    set_hold_flag(camp.id, note="需要安装 ffmpeg", campaigns_dir=camps)

    from haa.cli.main import app
    runner = CliRunner()
    r = runner.invoke(app, ["project", "resume", "proj_a5"])
    assert r.exit_code == 0, r.output

    events = store.list_events(proj.id)
    resumed = [e for e in events if e.event_type == "hold_resumed"]
    assert len(resumed) == 1
    assert (resumed[0].cost_usd or 0) == 0
