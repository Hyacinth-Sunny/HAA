"""Tests for P2: DebugSession dual-phase circuit breaker + ProjectController P2 orchestration (v0.7).

Uses FakeTransport (scripted crash/metrics sequences) + FakeCodingAgent so no
real SSH / Claude Code / LLM calls are made.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from haa.config import default_config
from haa.models import (
    Brief,
    Phase,
    Precursor,
    Project,
    ProjectHyperparams,
    ProjectStatus,
    Track,
)
from haa.p2.debug_session import DebugConfig, DebugResult, DebugSession
from haa.p2.transport import LocalDebugTransport, RunResult
from haa.project_controller import ProjectController
from haa.state import StateStore


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #

class FakeTransport:
    """Scriptable transport: first ``crash_first`` runs crash, next
    ``bad_metrics_first`` runs produce bad metrics, then good metrics."""

    def __init__(
        self,
        base_dir: Path,
        *,
        crash_first: int = 0,
        bad_metrics_first: int = 0,
        good_metrics: dict | None = None,
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.crash_first = crash_first
        self.bad_metrics_first = bad_metrics_first
        self.good_metrics = good_metrics or {"final_loss": 0.05, "accuracy": 0.95}
        self.run_count = 0
        self.deploy_count = 0

    def deploy(self, code_dir: str | Path, run_id: str) -> str:
        self.deploy_count += 1
        exec_dir = self.base_dir / run_id
        if exec_dir.exists():
            shutil.rmtree(exec_dir)
        exec_dir.mkdir(parents=True)
        return str(exec_dir)

    def run(self, exec_dir: str, entry_command: str, *, timeout: int | None = None) -> RunResult:
        self.run_count += 1
        results_dir = Path(exec_dir) / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        if self.run_count <= self.crash_first:
            return RunResult(
                exit_code=1,
                stderr="Traceback (most recent call last):\n  ImportError: No module",
                results_dir=results_dir,
            )
        # Not crashing — write metrics.
        non_crash_round = self.run_count - self.crash_first
        if non_crash_round <= self.bad_metrics_first:
            metrics = {"final_loss": float("inf")}  # bad
        else:
            metrics = self.good_metrics
        (results_dir / "metrics.json").write_text(json.dumps(metrics))
        return RunResult(exit_code=0, stdout="Training complete", results_dir=results_dir)

    def download_results(self, exec_dir: str, local_dir: str | Path) -> Path:
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        src = Path(exec_dir) / "results" / "metrics.json"
        if src.exists():
            shutil.copy2(str(src), str(local_dir / "metrics.json"))
        return local_dir


class FakeCodingAgent:
    """Records fix/diagnose calls; generate_code returns a real code dir."""

    def __init__(self, code_dir: Path | None = None):
        self.code_dir = code_dir
        self.fix_calls: list[str] = []
        self.diagnose_calls: list[dict] = []
        self.generate_calls: list[dict] = []

    def generate_code(self, paper_precursor: dict, exp_spec: dict, **kw):
        from haa.coding_agent import ACPResult

        self.generate_calls.append({"paper": paper_precursor, "exp_spec": exp_spec})
        if self.code_dir is not None:
            self.code_dir.mkdir(parents=True, exist_ok=True)
        return ACPResult(success=True, output="code written", work_dir=self.code_dir)

    def fix_traceback(self, traceback_text: str, **kw):
        from haa.coding_agent import ACPResult

        self.fix_calls.append(traceback_text)
        return ACPResult(success=True)

    def diagnose_metrics(self, metrics: dict, log_tail: str, **kw):
        from haa.coding_agent import ACPResult

        self.diagnose_calls.append({"metrics": metrics, "log": log_tail})
        return ACPResult(success=True)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class MockLLM:
    """Returns a fixed JSON analysis."""

    def __init__(self, analysis: str = '{"summary": "ok", "conclusion": "good"}'):
        self._content = analysis

    def call(self, messages, *, stage=None, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(content=self._content)


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def work_tmp(tmp_path):
    return tmp_path


@pytest.fixture
def debug_config():
    return DebugConfig(
        max_hard_error_rounds=5,  # smaller for faster tests
        max_logic_error_rounds=3,
        early_stop_patience=10,  # disable early-stop in basic tests
        entry_command="python main.py",
    )


@pytest.fixture
def code_dir(work_tmp):
    d = work_tmp / "code"
    d.mkdir()
    (d / "main.py").write_text("print('hello')")
    return d


@pytest.fixture
def coding_agent():
    return FakeCodingAgent()


# --------------------------------------------------------------------------- #
#  DebugSession tests
# --------------------------------------------------------------------------- #

class TestDebugSessionPhaseA:

    def test_phase_a_passes_first_try(self, work_tmp, code_dir, coding_agent, debug_config):
        """No crashes → Phase A passes in 1 round → Phase B passes (good metrics)."""
        transport = FakeTransport(work_tmp / "exec", crash_first=0, bad_metrics_first=0)
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=debug_config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert result.success
        assert result.phase == "done"
        assert result.rounds_a == 1
        assert result.rounds_b == 1
        assert result.metrics["final_loss"] == 0.05

    def test_phase_a_fixes_then_passes(self, work_tmp, code_dir, coding_agent, debug_config):
        """2 crashes → CC fixes → Phase A passes on round 3."""
        transport = FakeTransport(work_tmp / "exec", crash_first=2, bad_metrics_first=0)
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=debug_config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert result.success
        assert result.rounds_a == 3  # 2 crashes + 1 success
        assert len(coding_agent.fix_calls) == 2

    def test_phase_a_circuit_breaker(self, work_tmp, code_dir, coding_agent):
        """All rounds crash → circuit breaker trips."""
        config = DebugConfig(max_hard_error_rounds=3, max_logic_error_rounds=3)
        transport = FakeTransport(work_tmp / "exec", crash_first=100)  # always crash
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert not result.success
        assert result.reason == "phase_a_circuit_breaker"
        assert result.rounds_a == 3
        assert result.circuit_breaker_tripped


class TestDebugSessionPhaseB:

    def test_phase_b_fixes_then_passes(self, work_tmp, code_dir, coding_agent, debug_config):
        """Phase A passes instantly; Phase B: 1 bad-metric round → good.

        With crash_first=0, Phase A consumes non_crash_round 1 (passes, doesn't
        check metrics). Phase B then sees non_crash_round 2 (bad) → diagnose,
        then non_crash_round 3 (good) → success. So rounds_b=2, 1 diagnose call.
        """
        transport = FakeTransport(work_tmp / "exec", crash_first=0, bad_metrics_first=2)
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=debug_config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert result.success
        assert result.rounds_a == 1  # Phase A: 1 round (no crash)
        assert result.rounds_b == 2  # 1 bad + 1 good
        assert len(coding_agent.diagnose_calls) == 1

    def test_phase_b_circuit_breaker(self, work_tmp, code_dir, coding_agent):
        """Phase B never produces good metrics → circuit breaker."""
        config = DebugConfig(max_hard_error_rounds=3, max_logic_error_rounds=2)
        transport = FakeTransport(work_tmp / "exec", crash_first=0, bad_metrics_first=100)
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert not result.success
        assert result.reason == "phase_b_circuit_breaker"
        assert result.circuit_breaker_tripped

    def test_phase_b_nan_metrics_treated_as_bad(self, work_tmp, code_dir, coding_agent, debug_config):
        """NaN in metrics → not reasonable → keeps debugging."""
        transport = FakeTransport(
            work_tmp / "exec", crash_first=0, bad_metrics_first=100,
            good_metrics={"final_loss": float("nan")},
        )
        session = DebugSession(
            transport=transport, coding_agent=coding_agent, config=debug_config,
            code_dir=code_dir, work_dir=work_tmp / "debug", campaign_id="camp1",
        )
        result = session.run()
        assert not result.success  # NaN never counts as reasonable


# --------------------------------------------------------------------------- #
#  LocalDebugTransport smoke test
# --------------------------------------------------------------------------- #

class TestLocalDebugTransport:

    def test_runs_real_python_script(self, tmp_path):
        """LocalDebugTransport executes a real Python script and collects results."""
        code = tmp_path / "code"
        code.mkdir()
        (code / "main.py").write_text(
            "import json, os\n"
            "os.makedirs('results', exist_ok=True)\n"
            "json.dump({'loss': 0.1}, open('results/metrics.json', 'w'))\n"
        )
        transport = LocalDebugTransport(tmp_path / "exec")
        exec_dir = transport.deploy(str(code), "run1")
        # sys.executable: 裸 "python" 依赖 PATH（conda 未 activate 时 /bin/sh 找不到）
        import sys as _sys

        result = transport.run(exec_dir, f"{_sys.executable} main.py", timeout=30)
        assert result.exit_code == 0
        downloaded = transport.download_results(exec_dir, tmp_path / "dl")
        metrics = json.loads((downloaded / "metrics.json").read_text())
        assert metrics["loss"] == 0.1

    def test_crash_detection(self, tmp_path):
        """A script that exits non-zero is detected as crashed."""
        code = tmp_path / "code"
        code.mkdir()
        (code / "main.py").write_text("import sys; sys.exit(1)")
        transport = LocalDebugTransport(tmp_path / "exec")
        exec_dir = transport.deploy(str(code), "run1")
        result = transport.run(exec_dir, "python main.py", timeout=10)
        assert result.crashed


# --------------------------------------------------------------------------- #
#  ProjectController P2 orchestration
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def cfg():
    return default_config()


def _setup_p2_project(store, cfg, brief=None):
    """Create a project that's ready for P2 (phase=ARV, precursor selected)."""
    from tests.test_project_controller import FakePipeline, _make_controller

    brief = brief or Brief(title="T", problem_area="AI", track=Track.THEORY)
    pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
    ctrl = _make_controller(store, cfg, pipeline=pipe)
    project = ctrl.create_project(brief)
    started = ctrl.start_project(project.id)
    assert started.phase == Phase.ARV
    assert len(started.precursors) >= 1
    precursor_campaign = started.precursors[0].campaign_id
    ctrl.select_precursor(project.id, precursor_campaign)
    return project, started.precursors[0]


class TestProjectControllerP2:

    def test_advance_to_p2_success(self, store, cfg, tmp_path):
        """Full P2 flow: CODE_GEN → EXECUTE → ANALYZE → EA."""
        project, precursor = _setup_p2_project(store, cfg)

        # Fake code dir + coding agent + transport.
        fake_code = tmp_path / "fake_code"
        fake_code.mkdir()
        (fake_code / "main.py").write_text("print('ok')")

        coding_agent = FakeCodingAgent(code_dir=fake_code)
        transport = FakeTransport(tmp_path / "exec", crash_first=0, bad_metrics_first=0)
        mock_llm = MockLLM()

        ctrl = ProjectController(
            cfg, store, budget=None, llm=mock_llm,
            coding_agent_factory=lambda p, wd: coding_agent,
            p2_transport=transport,
        )
        result = ctrl.advance_to_p2(project.id)

        assert result.phase == Phase.EA
        assert result.exp_code_dir  # code dir recorded
        assert result.exp_results.get("metrics", {}).get("final_loss") == 0.05
        assert "analysis" in result.exp_results
        assert len(coding_agent.generate_calls) == 1

    def test_advance_to_p2_moribund_on_circuit_breaker(self, store, cfg, tmp_path):
        """DebugSession circuit breaker → MORIBUND + diagnostic."""
        project, precursor = _setup_p2_project(store, cfg)

        fake_code = tmp_path / "fake_code"
        fake_code.mkdir()
        (fake_code / "main.py").write_text("print('ok')")

        coding_agent = FakeCodingAgent(code_dir=fake_code)
        # Transport that always crashes → Phase A circuit breaker.
        transport = FakeTransport(tmp_path / "exec", crash_first=100)
        mock_llm = MockLLM(analysis="diagnostic text")

        ctrl = ProjectController(
            cfg, store, budget=None, llm=mock_llm,
            coding_agent_factory=lambda p, wd: coding_agent,
            p2_transport=transport,
        )
        result = ctrl.advance_to_p2(project.id)

        assert result.status == ProjectStatus.MORIBUND
        assert "circuit_breaker" in result.moribund_reason
        assert result.moribund_diagnostic  # diagnostic was generated

    def test_advance_to_p2_rejects_wrong_phase(self, store, cfg):
        """advance_to_p2 only valid in ARV phase."""
        brief = Brief(title="T", problem_area="AI", track=Track.THEORY)
        ctrl = ProjectController(cfg, store, budget=None, llm=MockLLM())
        project = ctrl.create_project(brief)
        # Project is in PR phase, not ARV.
        with pytest.raises(ValueError, match="ARV"):
            ctrl.advance_to_p2(project.id)

    def test_advance_to_p2_requires_selected_precursor(self, store, cfg, tmp_path):
        """advance_to_p2 requires selected_precursor_campaign_id."""
        from tests.test_project_controller import FakePipeline, _make_controller

        brief = Brief(title="T", problem_area="AI", track=Track.THEORY)
        pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        started = ctrl.start_project(project.id)
        # Don't call select_precursor — no precursor selected.
        started.phase = Phase.ARV  # force into ARV without selection
        store.save_project(started)

        ctrl2 = ProjectController(cfg, store, budget=None, llm=MockLLM())
        with pytest.raises(ValueError, match="no precursor selected"):
            ctrl2.advance_to_p2(project.id)
