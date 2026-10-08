"""M2 核心件测试：run_experiment 工具（solve.sh 契约/双模式/metrics）、
PILOT 微阶段（四路判决路径）、管线 PILOT 转移与特性开关。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from haa.config import Config, HarnessConfig, StorageConfig
from haa.harness.registry import ToolError, ToolRegistry
from haa.harness.tools.native import apply_native_tools
from haa.llm.agent_loop import AgentLoopResult
from haa.models import Brief, Campaign, Candidate
from haa.stages.base import StageContext
from haa.stages.pilot import PilotStage


def _reg(tmp_path):
    reg = ToolRegistry()
    apply_native_tools(reg, allowed_roots=(tmp_path,))
    return reg


def _mk_experiment(tmp_path, *, ok=True, with_metrics=True, name="exp1"):
    ws = tmp_path / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "solve.sh").write_text("#!/bin/bash\necho run\n", encoding="utf-8")
    if with_metrics:
        (ws / "output" / "logs").mkdir(parents=True, exist_ok=True)
        metrics = {"acc": 0.87, "loss": 0.21} if ok else {"acc": 0.11}
        (ws / "output" / "metrics.json").write_text(
            json.dumps(metrics), encoding="utf-8")
    return ws


def test_run_experiment_lightweight_success(tmp_path):
    reg = _reg(tmp_path)
    ws = _mk_experiment(tmp_path)
    r = reg.invoke("run_experiment", {"workspace": str(ws), "container": False})
    assert r.meta["exit_code"] == 0 and r.meta["ok"] is True
    assert '"acc": 0.87' in r.content and "success" in r.content


def test_run_experiment_missing_solve_sh(tmp_path):
    reg = _reg(tmp_path)
    ws = tmp_path / "empty"
    ws.mkdir()
    with pytest.raises(ToolError, match="solve.sh not found"):
        reg.invoke("run_experiment", {"workspace": str(ws), "container": False})


def test_run_experiment_outside_sandbox(tmp_path):
    reg = _reg(tmp_path)
    outside = Path(tempfile.mkdtemp())  # 沙箱根之外的临时目录
    with pytest.raises(ToolError, match="outside sandbox roots"):
        reg.invoke("run_experiment", {"workspace": str(outside), "container": False})


_ERRLOG_SOLVE = "\n".join([
    "#!/bin/bash",
    "mkdir -p output/logs",
    "echo 'boom: bad lr' | tee output/logs/error.log >&2",
    "exit 3",
])


def test_run_experiment_failure_reads_error_log(tmp_path):
    reg = _reg(tmp_path)
    ws = tmp_path / "expfail"
    ws.mkdir()
    (ws / "solve.sh").write_text(_ERRLOG_SOLVE, encoding="utf-8")
    (ws / "output").mkdir(parents=True, exist_ok=True)
    (ws / "output" / "metrics.json").write_text("{}", encoding="utf-8")
    r = reg.invoke("run_experiment", {"workspace": str(ws), "container": False})
    assert r.meta["exit_code"] == 3 and r.meta["ok"] is False
    assert "boom: bad lr" in r.content  # error.log 尾部随行


def test_run_experiment_metrics_parse_error_tolerated(tmp_path):
    reg = _reg(tmp_path)
    ws = tmp_path / "expbad"
    ws.mkdir()
    (ws / "solve.sh").write_text("#!/bin/bash\nmkdir -p output\necho ok\n",
                                 encoding="utf-8")
    (ws / "output").mkdir(parents=True, exist_ok=True)
    (ws / "output" / "metrics.json").write_text("{broken", encoding="utf-8")
    r = reg.invoke("run_experiment", {"workspace": str(ws), "container": False})
    assert "_metrics_parse_error" in r.meta["metrics"]


def _docker_ready():
    from haa.harness.tools.sandbox import DockerSandbox
    try:
        import subprocess as _sp
        probe = _sp.run(["docker", "images", "-q", "p2-sandbox-base"],
                        capture_output=True, text=True, timeout=15)
        return probe.returncode == 0 and probe.stdout.strip() \
            and DockerSandbox().available()
    except Exception:  # noqa: BLE001
        return False


_CONTAINER_SOLVE = "\n".join([
    "#!/bin/bash",
    "set -e",
    "mkdir -p output",
    "python3 -c \"import json,pathlib;"
    "pathlib.Path('output/metrics.json').write_text("
    "json.dumps({'pilot_acc':0.92}))\"",
])


@pytest.mark.skipif(not _docker_ready(), reason="docker/镜像不可用")
def test_run_experiment_container_e2e(tmp_path):
    """M2 验收核心：一份 solve.sh 在容器沙箱端到端跑通并产出结构化结果。"""
    reg = _reg(tmp_path)
    ws = _mk_experiment(tmp_path, name="exp_container", with_metrics=False)
    (ws / "solve.sh").write_text(_CONTAINER_SOLVE, encoding="utf-8")
    r = reg.invoke("run_experiment", {"workspace": str(ws), "container": True,
                                      "timeout_s": 180})
    assert r.meta["exit_code"] == 0
    assert r.meta["metrics"].get("pilot_acc") == 0.92
    assert r.meta["mode"] == "container"


# ---------------- PILOT 阶段（四路判决） ----------------

class FakeAgent:
    def __init__(self, content):
        self.content = content
        self.prompts = []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return AgentLoopResult(content=self.content)


def _pilot_stage(tmp_path, content):
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "camps")))
    stage = PilotStage(llm=None, config=cfg)
    fake = FakeAgent(content)
    stage._make_agent_loop = lambda: fake
    return stage, fake


def _ctx():
    camp = Campaign(brief_hash="abc")
    cand = Candidate(campaign_id=camp.id, slug="pilot-cand", title="T",
                     significance=.7, win_odds=.6, difficulty=.4, queue_index=0,
                     positive_claim="claim", negative_claim="criteria")
    ctx = StageContext(brief=Brief(title="B", problem_area="P"), candidate=cand)
    ctx.extra["exp_spec"] = {"metrics": ["acc"]}
    return camp, ctx


PILOT_OK = json.dumps({
    "verdict": "supported", "metrics_seen": {"acc": 0.9},
    "evidence": "acc 0.9 达到判据方向与量级", "reason": "ok",
    "anchor_diff": {"consistent": ["无偏差"], "deviations": []}})


def test_pilot_supported_continues(tmp_path):
    stage, fake = _pilot_stage(tmp_path, PILOT_OK)
    camp, ctx = _ctx()
    result = stage.run(camp, ctx)
    assert result.status.value == "continue"
    assert result.data["verdict"] == "supported"
    assert "solve.sh" in fake.prompts[0] and "四路判决" in fake.prompts[0]


def test_pilot_not_supported_aborts_candidate(tmp_path):
    content = json.dumps({"verdict": "not_supported",
                          "metrics_seen": {"acc": 0.1},
                          "evidence": "acc 0.1 远低于判据", "reason": "早杀"})
    stage, _ = _pilot_stage(tmp_path, content)
    camp, ctx = _ctx()
    result = stage.run(camp, ctx)
    assert result.status.value == "abort_candidate"
    assert result.data["verdict"] == "not_supported"


def test_pilot_bad_verdict_falls_to_inconclusive(tmp_path):
    content = json.dumps({"verdict": "大概行吧"})
    stage, _ = _pilot_stage(tmp_path, content)
    camp, ctx = _ctx()
    result = stage.run(camp, ctx)
    assert result.data["verdict"] == "inconclusive"


# ---------------- 管线转移与特性开关 ----------------

def _pipeline(tmp_path, *, pilot_on):
    from haa.pipeline import Pipeline, StageName
    from haa.state import StateStore
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "camps")),
                 harness=HarnessConfig(features=(("pilot", pilot_on),)))
    store = StateStore(tmp_path / "t.db")

    class FeasPass:
        from haa.stages.base import StageResult, StageStatus

        def run(self, campaign, context):
            return self.StageResult(
                status=self.StageStatus.CONTINUE,
                data={"blockers": [], "assessment": "pass"})

    stages = {StageName.SEEK: MagicMock(),
              StageName.EXP_SPEC: MagicMock(),
              StageName.EXP_FEASIBILITY: FeasPass(),
              StageName.PILOT: MagicMock()}
    return Pipeline(cfg, store, MagicMock(), llm=MagicMock(), stages=stages)


def test_pipeline_pass_goes_pilot_when_enabled(tmp_path):
    from haa.pipeline import StageName
    pipe = _pipeline(tmp_path, pilot_on=True)
    result = pipe._after_exp_feasibility(
        SimpleNamespace(data={"blockers": []}), None,
        SimpleNamespace(extra={}))
    assert result == StageName.PILOT


def test_pipeline_pass_skips_pilot_when_disabled(tmp_path):
    from haa.pipeline import StageName
    pipe = _pipeline(tmp_path, pilot_on=False)
    result = pipe._after_exp_feasibility(
        SimpleNamespace(data={"blockers": []}), None,
        SimpleNamespace(extra={}))
    assert result == StageName.HUMAN_REVIEW


def test_pipeline_after_pilot_inconclusive_flags_then_human(tmp_path):
    from haa.pipeline import StageName
    pipe = _pipeline(tmp_path, pilot_on=True)
    ctx = SimpleNamespace(extra={})
    nxt = pipe._after_pilot(
        SimpleNamespace(data={"verdict": "inconclusive"}), None, ctx)
    assert nxt == StageName.HUMAN_REVIEW and ctx.extra["pilot_inconclusive"]
    nxt2 = pipe._after_pilot(
        SimpleNamespace(data={"verdict": "supported"}), None,
        SimpleNamespace(extra={}))
    assert nxt2 == StageName.HUMAN_REVIEW
