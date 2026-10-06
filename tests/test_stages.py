"""Phase 3 stage tests — each stage driven by a fake AgentLoop.

The LLM is replaced by a fake agent whose ``run`` returns a canned JSON string;
the stage's real logic (prompt rendering, JSON parsing, StageResult building,
Lesson-3 rollback) is exercised end-to-end. ``_make_agent_loop`` is monkey-
patched so no network/LLM is touched.
"""

from __future__ import annotations

import json

import pytest

from haa.config import load_config
from haa.llm.agent_loop import AgentLoopResult
from haa.models import Brief, Campaign, Candidate, CandidateStatus, GradeVerdict
from haa.stages.base import StageContext, StageResult, StageStatus
from haa.stages import (
    DesignStage, GradeStage, NoveltyStage, RefineStage, ReviewStage,
    ScreenStage, SeekStage, VerifyStage, WriteStage,
)


# --- test doubles ------------------------------------------------------------


class FakeAgent:
    """Returns a canned AgentLoopResult; records each run() call."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def run(self, prompt, *, system_prompt=None, stage_name="", campaign_id="",
            max_tool_calls=10, json_mode=False, deadline_s=None):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "stage_name": stage_name}
        )
        return AgentLoopResult(content=self.content)


def _stage(cls, monkeypatch, content):
    """Build a stage wired to a FakeAgent returning `content` (JSON string)."""
    s = cls(llm=None, config=load_config())
    fake = FakeAgent(content)
    monkeypatch.setattr(s, "_make_agent_loop", lambda: fake)
    return s, fake


def _campaign():
    return Campaign(brief_hash="abc")


def _brief():
    return Brief(title="Unique Brief", problem_area="some area")


def _candidate(campaign):
    return Candidate(
        campaign_id=campaign.id, slug="cand", title="Cand",
        significance=0.7, win_odds=0.6, difficulty=0.4, queue_index=0,
        positive_claim="we achieve X", negative_claim="Omega(X) lower bound",
    )


# --- SEEK --------------------------------------------------------------------

def test_seek_builds_candidates_with_claims(monkeypatch):
    content = json.dumps({"ideas": [
        {"title": "Idea A", "slug": "idea-a", "positive_claim": "PC", "negative_claim": "NC",
         "significance": 0.8, "win_odds": 0.6, "difficulty": 0.4, "rationale": "r",
         "attack_plan": "ap", "closest_prior_work": "none"},
    ]})
    stage, fake = _stage(SeekStage, monkeypatch, content)
    camp = _campaign()
    result = stage.run(camp, StageContext(brief=_brief()))

    assert result.status == StageStatus.CONTINUE
    cands = result.data["candidates"]
    assert len(cands) == 1
    assert cands[0].positive_claim == "PC"
    assert cands[0].negative_claim == "NC"
    assert cands[0].rationale == "r"
    # stage_name forwarded; prompt carries the brief
    assert fake.calls[0]["stage_name"] == "SEEK"
    assert "Unique Brief" in fake.calls[0]["prompt"]


def test_seek_no_ideas_aborts_campaign(monkeypatch):
    stage, _ = _stage(SeekStage, monkeypatch, json.dumps({"ideas": []}))
    result = stage.run(_campaign(), StageContext(brief=_brief()))
    assert result.status == StageStatus.ABORT_CAMPAIGN


# --- NOVELTY -----------------------------------------------------------------

def test_novelty_new_continues(monkeypatch):
    stage, _ = _stage(NoveltyStage, monkeypatch,
                      json.dumps({"verdict": "NEW", "closest_prior_work": "none"}))
    camp = _campaign(); cand = _candidate(camp)
    result = stage.run(camp, StageContext(brief=_brief(), candidate=cand))
    assert result.status == StageStatus.CONTINUE
    assert result.data["verdict"] == "NEW"


def test_novelty_solved_aborts_and_records_prior_work(monkeypatch):
    stage, _ = _stage(NoveltyStage, monkeypatch,
                      json.dumps({"verdict": "SOLVED", "closest_prior_work": "doi/123"}))
    camp = _campaign(); cand = _candidate(camp)
    result = stage.run(camp, StageContext(brief=_brief(), candidate=cand))
    assert result.status == StageStatus.ABORT_CANDIDATE
    assert cand.closest_prior_work == "doi/123"


# --- SCREEN ------------------------------------------------------------------

def test_screen_survives(monkeypatch):
    stage, _ = _stage(ScreenStage, monkeypatch, json.dumps({"survives": True}))
    camp = _campaign()
    result = stage.run(camp, StageContext(candidate=_candidate(camp)))
    assert result.status == StageStatus.CONTINUE
    assert result.data["survives"] is True


def test_screen_kill_with_evidence_aborts(monkeypatch):
    stage, _ = _stage(ScreenStage, monkeypatch, json.dumps({
        "survives": False, "kill_method": "explicit_counterexample",
        "evidence": "n=2 fails (script output)",
    }))
    result = stage.run(_campaign(), StageContext(candidate=_candidate(_campaign())))
    assert result.status == StageStatus.ABORT_CANDIDATE


def test_screen_kill_without_evidence_is_overridden_to_survive(monkeypatch):
    """No hard evidence ⇒ must let it through (HM-Pro: don't kill real ideas)."""
    stage, _ = _stage(ScreenStage, monkeypatch, json.dumps({
        "survives": False, "kill_method": "none", "evidence": "",
    }))
    result = stage.run(_campaign(), StageContext(candidate=_candidate(_campaign())))
    assert result.status == StageStatus.CONTINUE
    assert result.data["survives"] is True  # auto-overridden


# --- DESIGN / VERIFY ---------------------------------------------------------

def test_design_continues_with_plan(monkeypatch):
    stage, _ = _stage(DesignStage, monkeypatch, json.dumps({
        "plan": "divide and conquer", "positive_claim": "pc", "negative_claim": "nc",
        "obligations": ["o1"], "key_lemmas": ["l1"],
        "addresses_counterexamples": [], "attack_plan": "ap",
    }))
    result = stage.run(_campaign(), StageContext(candidate=_candidate(_campaign())))
    assert result.status == StageStatus.CONTINUE
    assert result.data["plan"] == "divide and conquer"
    assert result.data["obligations"] == ["o1"]


def test_verify_passes_when_no_counterexample(monkeypatch):
    stage, _ = _stage(VerifyStage, monkeypatch, json.dumps({"counterexamples": []}))
    ctx = StageContext(candidate=_candidate(_campaign()), design={"plan": "p"})
    result = stage.run(_campaign(), ctx)
    assert result.data["verify_passed"] is True
    assert result.findings == []


def test_verify_fails_with_counterexample(monkeypatch):
    stage, _ = _stage(VerifyStage, monkeypatch,
                      json.dumps({"counterexamples": ["breaks for n=2"]}))
    ctx = StageContext(candidate=_candidate(_campaign()), design={"plan": "p"})
    result = stage.run(_campaign(), ctx)
    assert result.data["verify_passed"] is False
    assert len(result.findings) == 1
    assert result.findings[0]["detail"] == "breaks for n=2"


# --- GRADE -------------------------------------------------------------------

def test_grade_solid_continues(monkeypatch):
    stage, _ = _stage(GradeStage, monkeypatch, json.dumps({"grade": "solid", "rationale": "r"}))
    camp = _campaign(); cand = _candidate(camp)
    result = stage.run(camp, StageContext(candidate=cand, design={"plan": "p"}))
    assert result.status == StageStatus.CONTINUE
    assert cand.grade == GradeVerdict.SOLID


def test_grade_trivial_aborts(monkeypatch):
    stage, _ = _stage(GradeStage, monkeypatch, json.dumps({"grade": "trivial"}))
    camp = _campaign(); cand = _candidate(camp)
    result = stage.run(camp, StageContext(candidate=cand, design={"plan": "p"}))
    assert result.status == StageStatus.ABORT_CANDIDATE
    assert cand.grade == GradeVerdict.TRIVIAL


def test_grade_unknown_label_defaults_to_solid(monkeypatch):
    """HM-Pro: when unsure, do NOT downgrade — default is solid."""
    stage, _ = _stage(GradeStage, monkeypatch, json.dumps({"grade": "???"}))
    camp = _campaign(); cand = _candidate(camp)
    result = stage.run(camp, StageContext(candidate=cand, design={"plan": "p"}))
    assert result.status == StageStatus.CONTINUE
    assert cand.grade == GradeVerdict.SOLID


# --- WRITE -------------------------------------------------------------------

def test_write_extracts_sections(monkeypatch):
    """v1.0.5 论文前半部：只产 abstract/intro/background/method 四节。"""
    stage, _ = _stage(WriteStage, monkeypatch, json.dumps({
        "title": "T", "abstract": "a", "intro": "i", "background": "b",
        "method": "m", "eval": "e", "related": "r", "conclusion": "c",
        "self_negation_scan": [{"phrase": "we do not claim", "action": "revised"}],
    }))
    result = stage.run(_campaign(), StageContext(candidate=_candidate(_campaign()), design={"plan": "p"}))
    assert result.data["method"] == "m"
    assert result.data["background"] == "b"
    assert result.data["title"] == "T"
    assert result.data["self_negation_scan"][0]["action"] == "revised"
    # 前半部之外的字段不再进入 paper（实验设计属 exp_spec 工件）
    assert "conclusion" not in result.data
    assert "eval" not in result.data
    assert "related" not in result.data


# --- REVIEW ------------------------------------------------------------------

def test_review_aggregates_three_scores_accept(monkeypatch):
    stage, fake = _stage(ReviewStage, monkeypatch, json.dumps({
        "score": 0.9, "verdict": "v", "major_issues": [], "minor_issues": [],
    }))
    paper = {"title": "T", "abstract": "a", "method": "m"}
    result = stage.run(_campaign(), StageContext(candidate=_candidate(_campaign()), paper=paper))
    assert result.data["decision"] == "accept"
    assert result.data["overall"] == pytest.approx(0.9)
    # v1.0.3：四路 lens（fidelity 审"是否兑现简报委托"）
    assert set(result.data["scores"]) == {"correctness", "quality", "industry", "fidelity"}
    # four independent review calls, each got the blinded paper as the prompt
    assert len(fake.calls) == 4
    assert all("trace" not in c["prompt"] for c in fake.calls)  # blinded
    assert all(c["prompt"].count("m") >= 1 for c in fake.calls)


def test_review_rejects_when_low(monkeypatch):
    stage, _ = _stage(ReviewStage, monkeypatch, json.dumps({"score": 0.2}))
    result = stage.run(_campaign(), StageContext(paper={"title": "T"}))
    assert result.data["decision"] == "reject"


# --- REFINE (Lesson 3 rollback) ---------------------------------------------

def test_refine_rolls_back_to_best_when_regressed(monkeypatch):
    """Latest review worse than best snapshot ⇒ restore best before refining."""
    stage, _ = _stage(RefineStage, monkeypatch, json.dumps({
        "method": "REFINED", "addressed": [{"lens": "correctness", "issue": "x", "fix": "y"}],
    }))
    camp = _campaign()
    ctx = StageContext(candidate=_candidate(camp))
    ctx.paper = {"title": "low", "method": "LOW_VERSION"}
    ctx.review = {"overall": 0.3}  # latest review regressed
    ctx.best_snapshot = {
        "paper": {"title": "high", "method": "HIGH_VERSION"},
        "review": {"overall": 0.9},
    }
    result = stage.run(camp, ctx)
    # rolled back to best (title preserved), then method refined
    assert result.data["title"] == "high"
    assert result.data["method"] == "REFINED"
    assert result.data["addressed"][0]["fix"] == "y"


def test_refine_no_rollback_when_no_snapshot(monkeypatch):
    stage, _ = _stage(RefineStage, monkeypatch, json.dumps({"method": "NEW"}))
    camp = _campaign()
    ctx = StageContext(candidate=_candidate(camp))
    ctx.paper = {"title": "orig", "method": "ORIG"}
    result = stage.run(camp, ctx)
    assert result.data["title"] == "orig"
    assert result.data["method"] == "NEW"


# --- v1.0.3 指令遵循：简报铁律块必须抵达每个阶段 --------------------------------

IRON = "ML 只能进优化层，绝不进正确性层"


def _brief_ctx(camp):
    from haa.models import Brief

    return StageContext(
        candidate=_candidate(camp),
        brief=Brief(
            title="T", problem_area="P", track="systems",
            constraints=[IRON, "RTO 必须分桶报告"],
            exclusions=["不做更快的日志回放"],
            knowledge_files=["/notes/05-TwinBudget-双机预算分配.md"],
        ),
    )


def test_brief_block_reaches_design_and_verify(monkeypatch):
    """smoke5 教训复测：DESIGN/VERIFY 此前收不到简报→架构漂移无人拦。"""
    from haa.stages import DesignStage, VerifyStage

    for cls in (DesignStage, VerifyStage):
        stage, fake = _stage(cls, monkeypatch, "{}")
        camp = _campaign()
        ctx = _brief_ctx(camp)
        ctx.design = {"plan": "p", "obligations": []}
        try:
            stage.run(camp, ctx)
        except Exception:
            pass  # mock 返回 "{}"，后续解析失败无妨——只看 prompt
        prompt = fake.calls[0]["prompt"]
        assert IRON in prompt, f"{cls.__name__} 未收到简报铁律"
        assert "knowledge/05-TwinBudget" in prompt
        assert "不做更快的日志回放" in prompt


def test_design_prompt_contains_contract_when_brief_present(monkeypatch):
    from haa.stages import DesignStage

    stage, fake = _stage(DesignStage, monkeypatch, "{}")
    camp = _campaign()
    ctx = _brief_ctx(camp)
    try:
        stage.run(camp, ctx)
    except Exception:
        pass
    prompt = fake.calls[0]["prompt"]
    assert "设计契约" in prompt
    assert "系统模型锁死" in prompt


def test_review_fidelity_lens_gets_brief_block(monkeypatch):
    from haa.stages import ReviewStage

    stage, fake = _stage(ReviewStage, monkeypatch, json.dumps({
        "score": 0.9, "verdict": "v", "major_issues": [], "minor_issues": [],
    }))
    camp = _campaign()
    ctx = _brief_ctx(camp)
    ctx.paper = {"title": "T", "abstract": "a"}
    result = stage.run(camp, ctx)
    assert "fidelity" in result.data["scores"]
    # fidelity 的 system prompt 含简报铁律；论文文本进的是四路共用的 review_text
    assert any("简报符合性" in (c.get("system_prompt") or "") for c in fake.calls)
    assert all(IRON in c["prompt"] for c in fake.calls)


def test_review_threshold_from_config(monkeypatch):
    """v1.0.5：review_accept_threshold 可配置——0.62 分在 0.6 阈值下接受。"""
    from dataclasses import replace
    from haa.config import load_config

    cfg = replace(load_config(), pipeline=replace(
        load_config().pipeline, review_accept_threshold=0.6))
    stage = ReviewStage(llm=None, config=cfg)
    fake = FakeAgent(json.dumps({"score": 0.62, "verdict": "v",
                                 "major_issues": [], "minor_issues": []}))
    monkeypatch.setattr(stage, "_make_agent_loop", lambda: fake)
    result = stage.run(_campaign(), StageContext(paper={"title": "T"}))
    assert result.data["decision"] == "accept"
