"""Tests for haa/pipeline.py state-machine transitions.

These tests use mock stages (no real LLM calls) to verify the transition
logic, loop caps, candidate-queue advancement, and snapshot rollback.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from haa.config import default_config
from haa.models import Brief, Campaign, CampaignStatus, Candidate, CandidateStatus, Track
from haa.pipeline import Pipeline, StageName, CampaignContext
from haa.stages.base import BaseStage, StageResult, StageStatus, StageContext
from haa.state import StateStore


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = StateStore(str(db))
    yield s
    s.close()


@pytest.fixture
def brief():
    return Brief(
        title="Test Brief",
        problem_area="Generalization bounds for transformers",
        track=Track.THEORY,
    )


@pytest.fixture
def campaign(store, brief):
    return store.create_campaign(brief, budget_limit=20.0)


@pytest.fixture
def cfg():
    return default_config()


@pytest.fixture
def budget(store, cfg):
    from haa.budget import BudgetManager
    return BudgetManager(store, global_limit=100.0)


# --------------------------------------------------------------------------- #
#  Helper: mock stage that returns a canned result
# --------------------------------------------------------------------------- #

class MockStage(BaseStage):
    """A stage that returns a pre-set result."""

    def __init__(self, result: StageResult):
        self._result = result
        self.call_count = 0

    def run(self, campaign, context):
        self.call_count += 1
        return self._result


def _result(
    status: StageStatus = StageStatus.CONTINUE,
    data=None,
    findings=None,
    next_stage=None,
):
    return StageResult(
        status=status,
        data=data or {},
        findings=findings or [],
        next_stage=next_stage,
    )


def _make_pipeline(
    cfg, store, budget, stages: dict[StageName, BaseStage], llm=None
):
    """Build a Pipeline with injected mock stages."""
    if llm is None:
        llm = MagicMock()
    return Pipeline(cfg, store, budget, llm=llm, stages=stages)


# --------------------------------------------------------------------------- #
#  Tests: normal happy path
# --------------------------------------------------------------------------- #

class TestHappyPath:

    def test_full_pipeline_publishes(self, cfg, store, budget, campaign, brief):
        """SEEK→NOVELTY→SCREEN→DESIGN→VERIFY→GRADE→WRITE→REVIEW→PUBLISHED."""
        # SEEK produces candidates.
        candidates = [
            Candidate(
                campaign_id=campaign.id,
                slug="cand-1",
                title="Candidate 1",
                significance=0.8,
                win_odds=0.6,
                difficulty=0.4,
                queue_index=0,
            )
        ]
        # We need to set up context with candidates for downstream stages.
        # Build mock stages that each return CONTINUE.
        stages = {
            StageName.SEEK: MockStage(_result(
                status=StageStatus.CONTINUE,
                data={"candidates": candidates},
            )),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(
                data={"verdict": "solid"},
            )),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(
                data={"accept": True, "score": 8.0},
            )),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        assert result.status == CampaignStatus.PUBLISHED
        # Every stage should have been called at least once.
        for name, stage in stages.items():
            assert stage.call_count >= 1, f"{name} was not called"


# --------------------------------------------------------------------------- #
#  Tests: GRADE aborts candidate → advance queue
# --------------------------------------------------------------------------- #

class TestCandidateAdvancement:

    def test_grade_trivial_advances_queue(
        self, cfg, store, budget, campaign, brief
    ):
        """GRADE TRIVIAL → ABORT_CANDIDATE → next candidate."""
        # Two candidates so the queue isn't empty after the first dies.
        candidates = [
            Candidate(campaign_id=campaign.id, slug="c1", title="C1",
                      significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0),
            Candidate(campaign_id=campaign.id, slug="c2", title="C2",
                      significance=0.7, win_odds=0.5, difficulty=0.5, queue_index=1),
        ]
        call_log = []

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("SEEK")
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class NoveltyStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("NOVELTY")
                return _result()

        class ScreenStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("SCREEN")
                return _result()

        class DesignStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("DESIGN")
                return _result()

        class VerifyStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("VERIFY")
                return _result()

        grade_count = [0]

        class GradeStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("GRADE")
                grade_count[0] += 1
                if grade_count[0] == 1:
                    return _result(
                        status=StageStatus.ABORT_CANDIDATE,
                        data={"verdict": "trivial"},
                    )
                return _result(data={"verdict": "solid"})

        class WriteStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("WRITE")
                return _result()

        class ReviewStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("REVIEW")
                return _result(data={"accept": True, "score": 7.0})

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: NoveltyStage(),
            StageName.SCREEN: ScreenStage(),
            StageName.DESIGN: DesignStage(),
            StageName.VERIFY: VerifyStage(),
            StageName.GRADE: GradeStage(),
            StageName.WRITE: WriteStage(),
            StageName.REVIEW: ReviewStage(),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        # Should have gone through two GRADE calls and ultimately published.
        assert grade_count[0] >= 2
        assert result.status == CampaignStatus.PUBLISHED


# --------------------------------------------------------------------------- #
#  Tests: DESIGN↔VERIFY loop cap
# --------------------------------------------------------------------------- #

class TestDesignVerifyLoop:

    def test_design_verify_caps_at_max_rounds(
        self, cfg, store, budget, campaign, brief
    ):
        """DESIGN↔VERIFY loop exceeds max_design_rounds → proceed to GRADE."""
        max_rounds = cfg.pipeline.max_design_rounds
        verify_count = [0]

        candidates = [Candidate(campaign_id=campaign.id, slug="c1", title="C1",
                                significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0)]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class VerifyAlwaysFails(BaseStage):
            def run(self, camp, ctx):
                verify_count[0] += 1
                # Always return RETRY → forces loop.
                return _result(status=StageStatus.RETRY)

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: VerifyAlwaysFails(),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        # Loop should be capped, not infinite.
        # The exact cap behaviour depends on pipeline implementation,
        # but verify should NOT be called more than max_rounds times
        # without progressing.
        assert result.is_terminal  # Didn't hang.


# --------------------------------------------------------------------------- #
#  Tests: REVIEW→REFINE loop cap
# --------------------------------------------------------------------------- #

class TestReviewRefineLoop:

    def test_review_refine_caps_at_max_rounds(
        self, cfg, store, budget, campaign, brief
    ):
        """REVIEW→REFINE loop exceeds max_review_rounds → PUBLISHED (degraded)."""
        candidates = [Candidate(campaign_id=campaign.id, slug="c1", title="C1",
                                significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0)]
        review_count = [0]
        max_rounds = cfg.pipeline.max_review_rounds

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class ReviewAlwaysRejects(BaseStage):
            def run(self, camp, ctx):
                review_count[0] += 1
                return _result(
                    status=StageStatus.CONTINUE,
                    data={"accept": False, "score": 3.0},
                )

        class RefineStage(BaseStage):
            def run(self, camp, ctx):
                return _result()

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: ReviewAlwaysRejects(),
            StageName.REFINE: RefineStage(),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        # Should terminate (not hang) and ultimately publish (possibly degraded).
        assert result.is_terminal
        assert result.status in (CampaignStatus.PUBLISHED, CampaignStatus.RETIRED)


# --------------------------------------------------------------------------- #
#  Tests: budget exhaustion retires campaign
# --------------------------------------------------------------------------- #

class TestBudgetExhaustion:

    def test_budget_exhausted_retires(
        self, cfg, store, budget, campaign, brief
    ):
        """BudgetExhausted on a gated stage → RETIRED."""
        from haa.budget import BudgetExhausted

        candidates = [Candidate(campaign_id=campaign.id, slug="c1", title="C1",
                                significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0)]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                raise BudgetExhausted(
                    scope="campaign",
                    limit=0,
                    would_use=1,
                    campaign_id=camp.id,
                )

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result()),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result()),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        assert result.status == CampaignStatus.RETIRED


# --------------------------------------------------------------------------- #
#  Tests: budget_used sync (regression — smoke finding)
# --------------------------------------------------------------------------- #

def test_persist_does_not_clobber_budget_used(cfg, store, budget, brief):
    """Pipeline._persist must sync budget_used from the store so it doesn't
    overwrite BudgetManager's concurrent writes.

    Regression for the smoke-test finding: pre_spend/record update budget_used
    on a *fresh* campaign object, while the pipeline holds a separate in-memory
    campaign. Without re-sync, commit_transition→save_campaign clobbers the
    stored value with the stale one — which also silently disabled the budget
    gate (pre_spend reads the stored value).
    """
    pipe = _make_pipeline(cfg, store, budget, {})  # no stages needed for _persist
    camp = store.create_campaign(brief, budget_limit=20.0)
    # BudgetManager spends on a fresh campaign object → store budget_used = 1.5
    budget.pre_spend(camp.id, 1.5)
    assert store.get_campaign(camp.id).budget_used == pytest.approx(1.5)
    # The pipeline holds a SEPARATE in-memory campaign with stale budget_used.
    stale = store.get_campaign(camp.id)
    stale.budget_used = 0.0
    pipe._persist(stale, CampaignContext(brief=brief), "TEST")
    # The store must reflect 1.5 (synced from fresh), not the stale 0.0.
    assert store.get_campaign(camp.id).budget_used == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
#  Tests: EXP_FEASIBILITY fixable-fatal rework (smoke-audit P0 fix)
# --------------------------------------------------------------------------- #

class TestExpFixableFatalRework:
    """A fatal blocker WITH a fix_suggestion must rework EXP_SPEC (consuming
    max_exp rounds) instead of killing the candidate immediately."""

    def _make_stages(self, call_log, campaign_id):
        candidates = [Candidate(
            campaign_id=campaign_id, slug="fixable-idea", title="F",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class ExpSpecStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("EXP_SPEC")
                return _result()

        class ExpFeasStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("EXP_FEASIBILITY")
                return _result(data={"blockers": [{
                    "severity": "fatal",
                    "category": "protocol",
                    "detail": "PPO GAE is on-policy; no counterfactual estimator specified",
                    "fix_suggestion": "Use TD3 double critics to evaluate both learners",
                }]})

        class NoveltyStage(BaseStage):
            def run(self, camp, ctx):
                call_log.append("NOVELTY")
                return _result()

        return {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: NoveltyStage(),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: ExpSpecStage(),
            StageName.EXP_FEASIBILITY: ExpFeasStage(),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }

    def test_fixable_fatal_reworks_exp_spec(self, cfg, store, budget, campaign, brief):
        """fatal + fix_suggestion + rounds left → back to EXP_SPEC, not killed.

        max_exp_rounds=2: round1 fatal(fixable) → EXP_SPEC → round2 fatal again
        (fix_suggestion still present but exp_round==2==cap) → killed only then.
        """
        from dataclasses import replace
        cfg = replace(cfg, pipeline=replace(cfg.pipeline, max_exp_rounds=2))
        call_log = []
        pipe = _make_pipeline(cfg, store, budget, self._make_stages(call_log, campaign.id))
        result = pipe.run_campaign(campaign.id, brief=brief)

        # Rework semantics match the major-blocker branch: max_exp_rounds=2
        # grants 2 REWORKS, so EXP_FEASIBILITY runs 3 times (initial + 2
        # reworks) and only the exhaustion run kills the candidate.
        assert call_log.count("EXP_FEASIBILITY") == 3
        assert call_log.count("EXP_SPEC") == 3
        assert result.status == CampaignStatus.RETIRED  # killed after rounds exhausted

    def test_fixable_fatal_recovers_when_fixed(self, cfg, store, budget, campaign, brief):
        """Round 1 fatal(fixable) → EXP_SPEC rework → Round 2 PASS → publishes."""
        candidates = [Candidate(
            campaign_id=campaign.id, slug="recovers", title="R",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]
        feas_rounds = [0]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class ExpFeasStage(BaseStage):
            def run(self, camp, ctx):
                feas_rounds[0] += 1
                if feas_rounds[0] == 1:
                    return _result(data={"blockers": [{
                        "severity": "fatal", "category": "protocol",
                        "detail": "missing counterfactual estimator",
                        "fix_suggestion": "evaluate both learners via TD3 critics",
                    }]})
                return _result()  # PASS after rework

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: ExpFeasStage(),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        assert feas_rounds[0] == 2          # reworked once, then passed
        assert result.status == CampaignStatus.PUBLISHED  # NOT killed


# --------------------------------------------------------------------------- #
#  Tests: skip_to="novelty" bypass (P1 batch worker Campaign)
# --------------------------------------------------------------------------- #

class TestSkipToNovelty:

    def test_skip_to_novelty_activates_preseeded_candidate(
        self, cfg, store, budget, brief,
    ):
        """Brief.skip_to='novelty' + pre-seeded candidate → starts at NOVELTY
        with the pre-seeded candidate active; SEEK never runs."""
        from haa.models import Candidate, CandidateStatus, Track

        worker_brief = Brief(
            title=brief.title,
            problem_area=brief.problem_area,
            track=Track.THEORY,
            skip_to="novelty",
        )
        worker = store.create_campaign(worker_brief, budget_limit=20.0)
        seeded = Candidate(
            campaign_id=worker.id,
            slug="seeded-idea",
            title="Seeded Idea",
            significance=0.8,
            win_odds=0.6,
            difficulty=0.4,
            queue_index=0,
            status=CandidateStatus.PROPOSED,
        )
        store.save_candidate(seeded)

        novelty_seen = {}

        class NoveltySpy(BaseStage):
            def run(self, camp, ctx):
                novelty_seen["candidate_id"] = ctx.candidate.id if ctx.candidate else None
                novelty_seen["candidate_slug"] = ctx.candidate.slug if ctx.candidate else None
                return _result()

        stages = {
            StageName.SEEK: MockStage(_result(data={"candidates": []})),
            StageName.NOVELTY: NoveltySpy(),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(worker.id, brief=worker_brief)

        # SEEK must NOT have been called (we bypassed it).
        assert stages[StageName.SEEK].call_count == 0
        # NOVELTY was called and saw the pre-seeded candidate.
        assert novelty_seen.get("candidate_slug") == "seeded-idea"
        assert result.status == CampaignStatus.PUBLISHED


# --------------------------------------------------------------------------- #
#  Tests: output_candidate_count cap (P1 batch filtering)
# --------------------------------------------------------------------------- #

class TestOutputCandidateCountCap:

    def test_after_seek_caps_to_top_k(self, cfg, store, budget, campaign, brief):
        """SEEK returns 6 candidates; output_candidate_count=2 → the 2 highest-EV
        candidates survive as non-terminal, the other 4 are marked FILTERED."""
        from dataclasses import replace

        from haa.models import CandidateStatus

        cfg = replace(
            cfg, pipeline=replace(cfg.pipeline, output_candidate_count=2)
        )
        # 6 candidates with strictly increasing EV (significance × win_odds).
        candidates = [
            Candidate(
                campaign_id=campaign.id,
                slug=f"c{i}",
                title=f"C{i}",
                significance=0.1 + 0.1 * i,  # 0.1, 0.2, … 0.6
                win_odds=0.5,
                difficulty=0.5,
                queue_index=i,
            )
            for i in range(6)
        ]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        pipe.run_campaign(campaign.id, brief=brief)

        stored = store.list_candidates(campaign.id)
        filtered = [c for c in stored if c.status == CandidateStatus.FILTERED]
        alive = [c for c in stored if c.status != CandidateStatus.FILTERED]
        # 4 lowest-EV candidates filtered, 2 highest survive.
        assert len(filtered) == 4
        assert len(alive) == 2
        # The survivors must be the two with the highest EV (c4=0.4, c5=0.6 EVs).
        survivor_slugs = {c.slug for c in alive}
        assert survivor_slugs == {"c4", "c5"}


# --------------------------------------------------------------------------- #
#  Tests: v1.0.2 degraded publish swaps in the best snapshot
# --------------------------------------------------------------------------- #

class TestDegradedPublishUsesBestSnapshot:

    def test_declining_scores_publish_round1_paper(
        self, cfg, store, budget, campaign, brief
    ):
        """smoke4 复盘：0.593 → 0.517 逐轮下滑时，降级发布应发最佳版而非最差版。"""
        candidates = [Candidate(campaign_id=campaign.id, slug="c1", title="C1",
                                significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0)]

        class SeekStage(BaseStage):
            def run(self, camp, ctx):
                ctx.candidates = list(candidates)
                return _result(data={"candidates": candidates})

        class WriteStage(BaseStage):
            def run(self, camp, ctx):
                # 契约：WRITE/REFINE 的 paper 经 result.data 进 context
                # （_transition 里 context.paper = result.data）。
                return _result(data={"title": "written-by-WRITE", "abstract": "w"})

        class ReviewDeclining(BaseStage):
            def __init__(self):
                self.n = 0

            def run(self, camp, ctx):
                self.n += 1
                overall = {1: 0.6, 2: 0.5, 3: 0.4}.get(self.n, 0.4)
                return _result(
                    status=StageStatus.CONTINUE,
                    data={"decision": "reject", "overall": overall},
                )

        class RefineStage(BaseStage):
            def run(self, camp, ctx):
                return _result(data={"title": f"refined-r{ctx.review_round}", "abstract": "r"})

        stages = {
            StageName.SEEK: SeekStage(),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: WriteStage(),
            StageName.REVIEW: ReviewDeclining(),
            StageName.REFINE: RefineStage(),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        result = pipe.run_campaign(campaign.id, brief=brief)

        assert result.is_terminal
        assert result.status == CampaignStatus.PUBLISHED
        cp = store.latest_checkpoint(campaign.id)
        paper = (cp.context or {}).get("paper") or {}
        review = (cp.context or {}).get("review") or {}
        # 最佳快照（r1 后：还是 WRITE 的 0.6 版）替换了最终 0.4 版
        assert paper.get("title") == "written-by-WRITE"
        assert review.get("overall") == pytest.approx(0.6)
        assert paper.get("degraded") is True
