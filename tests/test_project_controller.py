"""Tests for ProjectController — the macro state machine + P1 batch orchestration.

These tests use a FakePipeline (no real LLM) to verify the scout + worker
Campaign orchestration, precursor harvesting, MORIBUND handling, and the
state-machine transitions.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from haa.config import default_config
from haa.models import (
    Brief,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    GradeVerdict,
    Phase,
    Project,
    ProjectHyperparams,
    ProjectStatus,
    Track,
)
from haa.project_controller import ProjectController
from haa.state import StateStore


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

class _Resp:
    """Minimal LLMResponse stand-in for the diagnostic call."""

    def __init__(self, content: str):
        self.content = content


class MockLLM:
    """LLM stub that returns a fixed diagnostic string."""

    def __init__(self, diagnostic: str = "Diagnostic: all ideas were trivial."):
        self.diagnostic = diagnostic
        self.calls: list[dict[str, Any]] = []

    def call(self, messages, *, stage=None, **kwargs):
        self.calls.append({"stage": stage, "n_messages": len(messages)})
        return _Resp(self.diagnostic)


class FakePipeline:
    """Mock pipeline that simulates run_campaign without real LLM calls.

    - Scout campaigns: seeds N candidates, marks the first PUBLISHED (or all DEAD
      if ``publish=False``), writes a checkpoint with paper/review data.
    - Worker campaigns (skip_to='novelty'): marks the pre-seeded candidate
      PUBLISHED, writes a checkpoint.
    - ``pause_first=True``: the first call for each campaign returns
      AWAITING_HUMAN_REVIEW (simulating the HUMAN_REVIEW gate); the second call
      proceeds normally.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        publish: bool = True,
        pause_first: bool = False,
        scout_candidate_count: int = 3,
    ):
        self.store = store
        self.publish = publish
        self.pause_first = pause_first
        self.scout_candidate_count = scout_candidate_count
        self.run_log: list[tuple[str, str | None]] = []
        self._paused: set[str] = set()
        self._scout_seeded: set[str] = set()

    def run_campaign(self, campaign_id: str, *, brief=None):
        skip_to = getattr(brief, "skip_to", None) if brief else None
        self.run_log.append((campaign_id, skip_to))
        campaign = self.store.get_campaign(campaign_id)

        # HUMAN_REVIEW pause simulation (first call only).
        if self.pause_first and campaign_id not in self._paused:
            self._paused.add(campaign_id)
            campaign.status = CampaignStatus.AWAITING_HUMAN_REVIEW
            self.store.save_campaign(campaign)
            self.store.save_checkpoint(campaign_id, "HUMAN_REVIEW:paused", {})
            return campaign

        # Scout: seed candidates if this is the first run (no skip_to).
        if skip_to != "novelty" and campaign_id not in self._scout_seeded:
            self._seed_scout_candidates(campaign_id)
            self._scout_seeded.add(campaign_id)

        cands = self.store.list_candidates(campaign_id)

        if self.publish:
            pub = cands[0] if cands else None
            if pub is not None:
                pub.status = CandidateStatus.PUBLISHED
                pub.grade = GradeVerdict.SOLID
                self.store.save_candidate(pub)
            campaign.status = CampaignStatus.PUBLISHED
            self.store.save_campaign(campaign)
            ctx = {
                "candidate": pub.model_dump(mode="json") if pub else {},
                "paper": {"abstract": "We show..."},
                "review": {"decision": "accept", "overall": 8.0},
                "extra": {"exp_spec": {"datasets": []}},
            }
            self.store.save_checkpoint(campaign_id, "PUBLISHED", ctx)
        else:
            for c in cands:
                c.status = CandidateStatus.DEAD
                self.store.save_candidate(c)
            campaign.status = CampaignStatus.RETIRED
            self.store.save_campaign(campaign)
            self.store.save_checkpoint(campaign_id, "RETIRED:queue_exhausted", {})

        return campaign

    def _seed_scout_candidates(self, campaign_id: str) -> None:
        for i in range(self.scout_candidate_count):
            self.store.save_candidate(Candidate(
                campaign_id=campaign_id,
                slug=f"scout-idea-{i}",
                title=f"Scout Idea {i}",
                significance=0.5 + 0.1 * i,
                win_odds=0.5,
                difficulty=0.5,
                queue_index=i,
            ))


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    # 测试隔离（CI lint 修复）：记忆库落 tmp——project_root 为只读 property 且
    # MemoryConfig 为 frozen dataclass，故用 dataclasses.replace 重建；Path 拼接
    # 中右操作数为绝对路径时胜出，memory_dir 指到 tmp 即可隔离，避免测试把
    # scout-idea 脏页写进真实 data/memory（CI 的 memory lint 步骤会被打挂）。
    import dataclasses
    c = default_config()
    c = dataclasses.replace(
        c, memory=dataclasses.replace(c.memory,
                                      memory_dir=str(tmp_path / "memory")))
    return c


@pytest.fixture
def brief():
    return Brief(title="Test", problem_area="AI", track=Track.THEORY)


def _make_controller(store, cfg, *, pipeline=None, llm=None):
    """Build a ProjectController with the given (or default) dependencies."""
    return ProjectController(cfg, store, budget=None, llm=llm, pipeline=pipeline)


# --------------------------------------------------------------------------- #
#  P1 batch orchestration
# --------------------------------------------------------------------------- #

class TestP1Batch:

    def test_scout_only_one_precursor(self, store, cfg, brief):
        """Scout PUBLISHED with 1 candidate, 0 remaining → ARV + 1 precursor."""
        pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        result = ctrl.start_project(project.id)

        assert result.status == ProjectStatus.IN_PROGRESS
        assert result.phase == Phase.ARV
        assert len(result.precursors) == 1
        assert result.precursors[0].grade == "solid"
        # Only the scout ran (no workers needed).
        assert len(pipe.run_log) == 1

    def test_spawns_workers_for_remaining(self, store, cfg, brief):
        """Scout PUBLISHED + 2 remaining PROPOSED → 2 workers + 3 precursors."""
        pipe = FakePipeline(store, publish=True, scout_candidate_count=3)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        result = ctrl.start_project(project.id)

        assert result.phase == Phase.ARV
        assert len(result.precursors) == 3  # 1 scout + 2 workers
        # Scout + 2 workers = 3 run_campaign calls.
        assert len(pipe.run_log) == 3
        # Workers used skip_to='novelty'.
        worker_calls = [(cid, sk) for cid, sk in pipe.run_log if sk == "novelty"]
        assert len(worker_calls) == 2

    def test_no_precursors_sets_moribund(self, store, cfg, brief):
        """All candidates die → MORIBUND + diagnostic."""
        pipe = FakePipeline(store, publish=False, scout_candidate_count=3)
        mock_llm = MockLLM("All ideas were trivial.")
        ctrl = _make_controller(store, cfg, pipeline=pipe, llm=mock_llm)
        project = ctrl.create_project(brief)
        result = ctrl.start_project(project.id)

        assert result.status == ProjectStatus.MORIBUND
        assert result.moribund_reason == "p1_no_precursors"
        assert "trivial" in result.moribund_diagnostic
        assert len(result.moribund_history) == 1
        assert len(mock_llm.calls) == 1
        assert mock_llm.calls[0]["stage"] == "P1_DIAGNOSTIC"

    def test_auto_approve_human_review(self, store, cfg, brief):
        """pause_first=True → each campaign pauses then auto-approves → PUBLISHED."""
        pipe = FakePipeline(
            store, publish=True, pause_first=True, scout_candidate_count=1
        )
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        result = ctrl.start_project(project.id)

        assert result.phase == Phase.ARV
        assert len(result.precursors) == 1
        # Scout: 2 calls (pause + resume). No workers (1 candidate).
        assert len(pipe.run_log) == 2

    def test_auto_approve_disabled_pauses_batch(self, store, cfg, brief):
        """auto_approve_human_review=False → campaign stays AWAITING_HUMAN_REVIEW."""
        pipe = FakePipeline(
            store, publish=True, pause_first=True, scout_candidate_count=1
        )
        hp = ProjectHyperparams(auto_approve_human_review=False)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief, hp)
        result = ctrl.start_project(project.id)

        # The scout paused at HUMAN_REVIEW and was NOT auto-approved.
        # No precursor harvested (not PUBLISHED yet).
        assert len(result.precursors) == 0
        # Only 1 call (paused, not resumed).
        assert len(pipe.run_log) == 1

    def test_worker_uses_pre_seeded_candidate(self, store, cfg, brief):
        """Worker campaigns must use skip_to='novelty' and the cloned candidate."""
        pipe = FakePipeline(store, publish=True, scout_candidate_count=2)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        ctrl.start_project(project.id)

        # Check linked campaigns.
        linked = store.list_campaigns_for_project(project.id)
        roles = {role for _, role in linked}
        assert roles == {"scout", "worker"}
        # The worker campaign should have exactly 1 pre-seeded candidate.
        worker_camps = [cid for cid, role in linked if role == "worker"]
        assert len(worker_camps) == 1
        worker_cands = store.list_candidates(worker_camps[0])
        assert len(worker_cands) == 1


# --------------------------------------------------------------------------- #
#  ARV transitions
# --------------------------------------------------------------------------- #

class TestARV:

    def test_select_precursor_records_choice(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=True, scout_candidate_count=2)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        started = ctrl.start_project(project.id)

        precursor_campaign = started.precursors[0].campaign_id
        result = ctrl.select_precursor(project.id, precursor_campaign)
        assert result.selected_precursor_campaign_id == precursor_campaign

    def test_select_precursor_rejects_wrong_phase(self, store, cfg, brief):
        ctrl = _make_controller(store, cfg, pipeline=FakePipeline(store))
        project = ctrl.create_project(brief)
        # Project is in PR phase, not ARV.
        with pytest.raises(ValueError, match="ARV"):
            ctrl.select_precursor(project.id, "fake")

    def test_rework_p1_clears_precursors(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=True, scout_candidate_count=2)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        started = ctrl.start_project(project.id)
        assert len(started.precursors) == 2

        # Rework with fewer candidates.
        pipe2 = FakePipeline(store, publish=True, scout_candidate_count=1)
        ctrl2 = _make_controller(store, cfg, pipeline=pipe2)
        result = ctrl2.rework_p1(project.id)
        assert len(result.precursors) == 1  # cleared + re-ran with 1 candidate
        assert result.phase == Phase.ARV


# --------------------------------------------------------------------------- #
#  MORIBUND recovery
# --------------------------------------------------------------------------- #

class TestMoribund:

    def test_recover_requires_confirmation(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=False, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe, llm=MockLLM())
        project = ctrl.create_project(brief)
        started = ctrl.start_project(project.id)
        assert started.status == ProjectStatus.MORIBUND

        with pytest.raises(ValueError, match="confirm_warning"):
            ctrl.recover_from_moribund(project.id)

        recovered = ctrl.recover_from_moribund(project.id, confirm_warning=True)
        assert recovered.status == ProjectStatus.IN_PROGRESS


# --------------------------------------------------------------------------- #
#  Terminal transitions
# --------------------------------------------------------------------------- #

class TestTerminal:

    def test_abort_from_in_progress(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        ctrl.start_project(project.id)

        result = ctrl.abort_project(project.id, reason="user stopped")
        assert result.status == ProjectStatus.ABORTED
        assert result.is_terminal

    def test_abort_from_moribund(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=False, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe, llm=MockLLM())
        project = ctrl.create_project(brief)
        started = ctrl.start_project(project.id)
        assert started.status == ProjectStatus.MORIBUND

        result = ctrl.abort_project(project.id)
        assert result.status == ProjectStatus.ABORTED

    def test_abort_rejects_already_terminal(self, store, cfg, brief):
        ctrl = _make_controller(store, cfg, pipeline=FakePipeline(store))
        project = ctrl.create_project(brief)
        ctrl.abort_project(project.id)

        with pytest.raises(ValueError, match="terminal"):
            ctrl.abort_project(project.id)

    def test_complete_requires_done_phase(self, store, cfg, brief):
        ctrl = _make_controller(store, cfg, pipeline=FakePipeline(store))
        project = ctrl.create_project(brief)
        with pytest.raises(ValueError, match="DONE"):
            ctrl.complete_project(project.id)


# --------------------------------------------------------------------------- #
#  Validation
# --------------------------------------------------------------------------- #

class TestValidation:

    def test_start_rejects_non_not_started(self, store, cfg, brief):
        ctrl = _make_controller(store, cfg, pipeline=FakePipeline(store))
        project = ctrl.create_project(brief)
        ctrl.abort_project(project.id)  # → ABORTED

        with pytest.raises(ValueError, match="NOT_STARTED"):
            ctrl.start_project(project.id)

    def test_get_project_returns_none_for_missing(self, store, cfg):
        ctrl = _make_controller(store, cfg, pipeline=FakePipeline(store))
        assert ctrl.get_project("nonexistent") is None


# --------------------------------------------------------------------------- #
#  Tests: v1.0.2 resume_p1_batch（断点恢复）
# --------------------------------------------------------------------------- #

class TestResumeP1Batch:

    def test_resume_after_crash_mid_worker(self, store, cfg, brief):
        """Crash 模拟：scout 完成 + worker1 跑到一半猝死 → resume 续跑
        worker1、补建候选3 的 worker、补 harvest scout 前体。"""
        from haa.models import ProjectStatus

        class CrashingPipeline(FakePipeline):
            calls = 0

            def run_campaign(self, campaign_id, *, brief=None):
                type(self).calls += 1
                if type(self).calls == 2:  # 第一个 worker：跑到一半进程猝死
                    c = self.store.get_campaign(campaign_id)
                    c.status = CampaignStatus.VERIFYING
                    self.store.save_campaign(c)
                    raise SystemExit(9)
                return super().run_campaign(campaign_id, brief=brief)

        pipe = CrashingPipeline(store, publish=True, scout_candidate_count=3)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        with pytest.raises(SystemExit):
            ctrl.start_project(project.id)

        # 猝死后的状态：项目仍在 P1，前体未落库
        fresh = store.get_project(project.id)
        assert fresh.phase == Phase.P1
        assert fresh.precursors == []

        result = ctrl.resume_p1_batch(project.id)
        assert result.phase == Phase.ARV
        assert result.status == ProjectStatus.IN_PROGRESS
        # scout + worker1（续跑）+ worker2（候选3 补建）三个前体都补齐
        assert len(result.precursors) == 3
        # 续跑期间 run_campaign 的调用：worker1 恢复 + worker2 新建 = 2 次
        # （崩溃的那次调用在 append run_log 之前就 raise 了，不在日志里）
        resume_calls = pipe.run_log[1:]
        assert len(resume_calls) == 2
        assert all(skip == "novelty" for _, skip in resume_calls)

    def test_resume_rejects_non_p1(self, store, cfg, brief):
        pipe = FakePipeline(store, publish=True, scout_candidate_count=1)
        ctrl = _make_controller(store, cfg, pipeline=pipe)
        project = ctrl.create_project(brief)
        ctrl.start_project(project.id)  # → ARV
        with pytest.raises(ValueError):
            ctrl.resume_p1_batch(project.id)
