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
        config=DebugConfig(max_hard_error_rounds=2, triage_enabled=True),
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
        config=DebugConfig(max_hard_error_rounds=1, triage_enabled=True),
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
    # B1 后 resume 走 _store() helper——补丁打在 haa.cli.main 命名空间
    # （call-time 全局查找，导入顺序无关）。
    monkeypatch.setattr("haa.cli.main._store", lambda: store)
    brief = Brief(title="T", problem_area="P")
    camp = store.create_campaign(brief)
    proj = Project(id="proj_a5", brief=brief,
                   selected_precursor_campaign_id=camp.id)
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


# ========== B1: HOLD 闭环（CLI 三处：scoping / _store helper / 状态转移） ==========

def _cli_env(tmp_path, monkeypatch):
    """B 组 CLI 测试环境：隔离 store + 旗标目录。"""
    from haa.state import StateStore

    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    store = StateStore(tmp_path / "t.db")
    monkeypatch.setattr("haa.cli.main._store", lambda: store)
    return store, camps


def _mk_proj_with_camp(store, brief, proj_id):
    from haa.models import Project

    camp = store.create_campaign(brief)
    proj = Project(id=proj_id, brief=brief,
                   selected_precursor_campaign_id=camp.id)
    store.save_project(proj)
    return proj, camp


def test_b1_hold_scoped_to_selected_campaign(tmp_path, monkeypatch):
    """hold 只作用于关联 campaign——多项目库中 hold A 不影响 B。"""
    from typer.testing import CliRunner

    store, camps = _cli_env(tmp_path, monkeypatch)
    brief = Brief(title="T", problem_area="P")
    proj_a, camp_a = _mk_proj_with_camp(store, brief, "projB_A")
    proj_b, camp_b = _mk_proj_with_camp(store, brief, "projB_B")

    from haa.cli.main import app
    runner = CliRunner()
    r = runner.invoke(app, ["project", "hold", "projB_A",
                            "--note", "observed stall"])
    assert r.exit_code == 0, r.output

    assert (camps / camp_a.id / "HOLD").exists()      # A 的旗标在
    assert not (camps / camp_b.id / "HOLD").exists()  # B 不受影响
    # 状态入库
    a = store.get_project(proj_a.id)
    b = store.get_project(proj_b.id)
    assert a.status == ProjectStatus.HOLD
    assert b.status != ProjectStatus.HOLD
    # 旗标内容=用户注记
    assert "observed stall" in (camps / camp_a.id / "HOLD").read_text(
        encoding="utf-8")


def test_b1_hold_no_association_errors(tmp_path, monkeypatch):
    """无关联 campaign → 报错退出（不碰任何旗标）。"""
    from typer.testing import CliRunner

    from haa.models import Project

    store, camps = _cli_env(tmp_path, monkeypatch)
    brief = Brief(title="T", problem_area="P")
    proj = Project(id="projB_no", brief=brief)  # 无 selected
    store.save_project(proj)

    from haa.cli.main import app
    runner = CliRunner()
    r = runner.invoke(app, ["project", "hold", "projB_no"])
    assert r.exit_code == 1
    assert "no associated campaign" in r.output
    # 全库零旗标
    assert not list(camps.rglob("HOLD"))


def test_b1_resume_transitions_and_event(tmp_path, monkeypatch):
    """DB 状态转移 IN_PROGRESS→HOLD→IN_PROGRESS + hold_resumed 事件。"""
    from typer.testing import CliRunner

    from haa.models import ProjectStatus

    from haa.cli.main import app

    store, camps = _cli_env(tmp_path, monkeypatch)
    brief = Brief(title="T", problem_area="P")
    proj, camp = _mk_proj_with_camp(store, brief, "projB_R")
    proj.status = ProjectStatus.IN_PROGRESS
    store.save_project(proj)

    runner = CliRunner()
    runner.invoke(app, ["project", "hold", "projB_R", "--note", "fix ffmpeg"])
    assert store.get_project(proj.id).status == ProjectStatus.HOLD

    r = runner.invoke(app, ["project", "resume", "projB_R"])
    assert r.exit_code == 0, r.output
    after = store.get_project(proj.id)
    assert after.status == ProjectStatus.IN_PROGRESS   # HOLD→IN_PROGRESS
    assert after.assist_note == "fix ffmpeg"           # 注记入库（B3）
    assert not (camps / camp.id / "HOLD").exists()     # 旗标已清
    events = store.list_events(proj.id)
    resumed = [e for e in events if e.event_type == "hold_resumed"]
    assert len(resumed) == 1
    assert resumed[0].payload.get("note_injected") is True
    assert resumed[0].payload.get("status_transitioned") is True


def test_b1_resume_without_hold_is_noop(tmp_path, monkeypatch):
    """无 HOLD 旗标且状态非 HOLD → 警告退出，不发事件、不动状态。"""
    from typer.testing import CliRunner

    from haa.cli.main import app

    store, camps = _cli_env(tmp_path, monkeypatch)
    brief = Brief(title="T", problem_area="P")
    proj, camp = _mk_proj_with_camp(store, brief, "projB_N")

    runner = CliRunner()
    r = runner.invoke(app, ["project", "resume", "projB_N"])
    assert r.exit_code == 0
    assert "nothing to resume" in r.output
    assert not [e for e in store.list_events(proj.id)
                if e.event_type == "hold_resumed"]


# ========== B2: check_hold_flag DB 分支（小写枚举值） ==========

def test_b2_check_hold_flag_db_branch():
    """campaign status="hold"（小写枚举值）→ DB 分支命中；大写存量兼容。"""
    from haa.p2revise import check_hold_flag

    for value in ("hold", "HOLD"):  # .lower() 比较两种都收
        fake_store = MagicMock()
        fake_store.get_campaign.return_value = SimpleNamespace(
            status=SimpleNamespace(value=value))
        assert check_hold_flag("c1", store=fake_store,
                               campaigns_dir="/nonexistent") is True
    fake_store2 = MagicMock()
    fake_store2.get_campaign.return_value = SimpleNamespace(
        status=SimpleNamespace(value="writing"))
    assert check_hold_flag("c1", store=fake_store2,
                           campaigns_dir="/nonexistent") is False


# ========== B3: 注记注入 assist_context ==========

def test_b3_note_injected_into_fix_material(tmp_path):
    """assist_context → 修复材料头部包含注记段（提示词组装处注入）。"""
    from haa.p2.debug_session import DebugConfig, DebugSession

    agent = MagicMock()
    session = DebugSession(
        transport=_CrashTransport(),
        coding_agent=agent,
        config=DebugConfig(max_hard_error_rounds=1),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c1",
        assist_context="需要先 sudo apt install ffmpeg",
    )
    session.run()
    material = agent.fix_traceback.call_args[0][0]
    assert "assist_context" in material
    assert "sudo apt install ffmpeg" in material


def test_b3_controller_passes_assist_note(tmp_path, monkeypatch):
    """project.assist_note → DebugSession(assist_context=…) 真实接线。"""
    from haa.models import Project
    from haa.project_controller import ProjectController
    from haa.state import StateStore

    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    proj = Project(id="proj_b3", brief=brief,
                   assist_note="swap 分区不足，请扩容")
    store.save_project(proj)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())

    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            from haa.p2.debug_session import DebugResult
            return DebugResult(success=False, phase="phase_a", rounds_a=1,
                               reason="phase_a_circuit_breaker", error="x")

    import haa.p2.debug_session as ds
    monkeypatch.setattr(ds, "DebugSession", _FakeSession)
    monkeypatch.setattr(ctrl, "_build_p2_transport", lambda: MagicMock())
    ctrl._p2_execute(proj, tmp_path / "code", MagicMock(), tmp_path)
    assert captured["assist_context"] == "swap 分区不足，请扩容"


# ========== C1: kill_reason 枚举映射 ==========

def _drive_moribund(tmp_path, reason: str):
    """真驱动 _set_moribund_p2（LLM 边界 mock）→ 返回 (project, store)。"""
    from haa.models import Project
    from haa.project_controller import ProjectController
    from haa.state import StateStore

    cfg = _cfg(tmp_path)
    store = StateStore(tmp_path / "t.db")
    brief = Brief(title="T", problem_area="P")
    proj = Project(id=f"proj_c1_{abs(hash(reason)) % 1000}", brief=brief)
    store.save_project(proj)
    ctrl = ProjectController(cfg, store, budget=None, llm=MagicMock())
    ctrl.llm.call.return_value.content = "(diagnostic text)"
    precursor = SimpleNamespace(
        campaign_id="cx", candidate_id="cd", candidate_slug="slug",
        candidate_title="T", exp_spec={}, paper={})
    debug = SimpleNamespace(
        success=False, phase="phase_a", rounds_a=15, rounds_b=0,
        reason=reason, error="boom", log="log tail", metrics={},
        results_dir=None)
    out = ctrl._set_moribund_p2(proj, debug, precursor)
    return out, store


def test_c1_kill_reason_cap_for_circuit_breaker(tmp_path):
    """轮数帽耗尽（circuit_breaker）→ 枚举 cap。"""
    from haa.models import ProjectStatus

    proj, _ = _drive_moribund(tmp_path, "phase_a_circuit_breaker")
    assert proj.status == ProjectStatus.MORIBUND
    assert proj.moribund_reason.startswith("cap:")
    assert "phase_a_circuit_breaker" in proj.moribund_reason  # 自由文本保留
    assert proj.moribund_history[-1].reason == proj.moribund_reason


def test_c1_kill_reason_budget(tmp_path):
    """预算耗尽 → 枚举 budget。"""
    proj, _ = _drive_moribund(tmp_path, "budget_exhausted")
    assert proj.moribund_reason.startswith("budget:")


def test_c1_kill_reason_code_for_early_stop(tmp_path):
    """早停（修不动）→ 枚举 code。"""
    proj, _ = _drive_moribund(tmp_path, "phase_b_early_stop")
    assert proj.moribund_reason.startswith("code:")


def test_c1_validate_kill_reason_new_format():
    """validate_kill_reason 对新格式/存量/legacy 的判决。"""
    from haa.p2revise import validate_kill_reason

    assert validate_kill_reason("cap:p2_phase_a_circuit_breaker")
    assert validate_kill_reason("budget:p2_budget_exhausted")
    assert validate_kill_reason("code:p2_phase_b_early_stop")
    assert validate_kill_reason("legacy:queue ran dry 2026-08")
    assert validate_kill_reason("viewpoint")            # 存量裸枚举
    assert not validate_kill_reason("pilot: not_supported")  # 非法前缀拒绝


# ========== C2: p2_triage 开关 ==========

def test_c2_switch_registered_default_off():
    """default.yaml 注册表含 p2_triage 且默认 false；DebugConfig 同步默认。"""
    from haa.config import load_config
    from haa.p2.debug_session import DebugConfig

    cfg = load_config()  # 无 HAA_CONFIG 时取 config/default.yaml
    assert ("p2_triage", False) in cfg.harness.features
    assert cfg.harness.feature("p2_triage") is False
    assert DebugConfig().triage_enabled is False


class _SudoCrashTransport:
    """伪执行后端：崩溃且日志带 sudo 密码签名（assist 类特征）。"""

    def deploy(self, code_dir, run_id):
        return Path(code_dir)

    def run(self, exec_dir, command, timeout=None):
        from haa.p2.transport import RunResult
        return RunResult(exit_code=1, stdout="",
                         stderr="sudo: a password is required")

    def download_results(self, exec_dir, results_dir):
        pass


def test_c2_triage_off_zero_change(tmp_path, monkeypatch):
    """开关关：sudo 签名也不触发 assist HOLD——纯修复循环到断路器
    （与批次15 之前行为一致；同输入同轮数不提前退出修复循环）。"""
    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    from haa.p2.debug_session import DebugConfig, DebugSession

    events: list[tuple[str, dict]] = []
    agent = MagicMock()
    session = DebugSession(
        transport=_SudoCrashTransport(),
        coding_agent=agent,
        config=DebugConfig(max_hard_error_rounds=3),  # triage_enabled=False
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c_off",
        event_sink=lambda etype, payload: events.append((etype, payload)),
    )
    result = session.run()
    # 旧出口序列：不提前退出——3 轮全走修复，断路器收尾
    assert result.reason == "phase_a_circuit_breaker"
    assert result.rounds_a == 3
    assert agent.fix_traceback.call_count == 3
    assert not events                      # 零分诊事件
    assert not (camps / "c_off" / "HOLD").exists()  # 未写 HOLD 旗标


def test_c2_triage_on_assist_hold(tmp_path, monkeypatch):
    """开关开：同输入 → 第一轮即 assist HOLD（提前退出修复循环）。"""
    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    from haa.p2.debug_session import DebugConfig, DebugSession

    agent = MagicMock()
    session = DebugSession(
        transport=_SudoCrashTransport(),
        coding_agent=agent,
        config=DebugConfig(max_hard_error_rounds=3, triage_enabled=True),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c_on",
    )
    result = session.run()
    assert result.reason == "assist_hold"
    flag = camps / "c_on" / "HOLD"
    assert flag.exists()
    assert "恢复" in flag.read_text(encoding="utf-8")  # 三要素求助请求
    assert agent.fix_traceback.call_count == 0  # 未进修复循环


def test_c2_phase_b_checkpoint(tmp_path, monkeypatch):
    """phase B 轮间检查点：phase A 执行期间落 HOLD 旗标 → B 轮首安全退出
    （rounds_b=真实已执行轮数=0）。"""
    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    from haa.p2.debug_session import DebugConfig, DebugSession
    from haa.p2.transport import RunResult

    class _FlagDuringPhaseA:
        def deploy(self, code_dir, run_id):
            if "_a_" in run_id:  # phase A 执行时落旗标（A 轮首检查已过）
                from haa.p2revise import set_hold_flag
                set_hold_flag("c_pb", note="user paused",
                              campaigns_dir=camps)
            return Path(code_dir)

        def run(self, exec_dir, command, timeout=None):
            return RunResult(exit_code=0, stdout="ok")

        def download_results(self, exec_dir, results_dir):
            pass

    session = DebugSession(
        transport=_FlagDuringPhaseA(),
        coding_agent=MagicMock(),
        config=DebugConfig(max_hard_error_rounds=3,
                           max_logic_error_rounds=3, triage_enabled=True),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c_pb",
    )
    result = session.run()
    assert not result.success
    assert result.reason == "hold_detected"
    assert result.phase == "phase_b"
    assert result.rounds_a == 1   # phase A 完成了 1 轮
    assert result.rounds_b == 0   # 检查点在 B 轮首——真实已执行 0 轮


def test_c2_phase_a_checkpoint_reports_real_rounds(tmp_path, monkeypatch):
    """HOLD 在 phase A 轮首命中 → rounds_a=0（真实已执行），非轮号 1。"""
    camps = _isolate_campaigns_dir(monkeypatch, tmp_path)
    from haa.p2.debug_session import DebugConfig, DebugSession
    from haa.p2revise import set_hold_flag

    set_hold_flag("c_r0", note="pre-set", campaigns_dir=camps)
    session = DebugSession(
        transport=_SudoCrashTransport(),
        coding_agent=MagicMock(),
        config=DebugConfig(max_hard_error_rounds=3, triage_enabled=True),
        code_dir=tmp_path / "code",
        work_dir=tmp_path / "work",
        campaign_id="c_r0",
    )
    result = session.run()
    assert result.reason == "hold_detected"
    assert result.rounds_a == 0  # D2 语义：真实已执行轮数（检查点在轮首）
