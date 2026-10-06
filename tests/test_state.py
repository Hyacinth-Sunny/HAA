"""Tests for the SQLite state store (create / restore / crash recovery)."""

import json

import pytest

from haa.models import Brief, Candidate, CandidateStatus, CampaignStatus
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


def _brief() -> Brief:
    return Brief(
        title="Upper bounds for fast conv",
        problem_area="convolution complexity",
        constraints=["must give a closed form"],
        exclusions=["no incremental future-work variants"],
    )


def _candidate(campaign_id: str, idx: int, slug: str) -> Candidate:
    return Candidate(
        campaign_id=campaign_id,
        slug=slug,
        title=f"Idea {idx}",
        significance=0.6,
        win_odds=0.5,
        difficulty=0.4,
        queue_index=idx,
    )


# --- create / round-trip --------------------------------------------------
def test_create_and_get_campaign(store):
    brief = _brief()
    campaign = store.create_campaign(brief, budget_limit=15.0)

    fetched = store.get_campaign(campaign.id)
    assert fetched is not None
    assert fetched.brief_hash == campaign.brief_hash == brief.brief_hash()
    assert fetched.status == CampaignStatus.QUEUED
    assert fetched.budget_limit == 15.0
    assert fetched.budget_used == 0.0


def test_save_campaign_upserts(store):
    campaign = store.create_campaign(_brief())
    campaign.status = CampaignStatus.DESIGNING
    campaign.budget_used = 3.25
    store.save_campaign(campaign)

    fetched = store.get_campaign(campaign.id)
    assert fetched.status == CampaignStatus.DESIGNING
    assert fetched.budget_used == pytest.approx(3.25)


def test_list_campaigns_ordered_by_creation(store):
    a = store.create_campaign(Brief(title="A", problem_area="pa"))
    b = store.create_campaign(Brief(title="B", problem_area="pb"))
    listed = store.list_campaigns()
    assert [c.id for c in listed] == [a.id, b.id]


# --- candidates -----------------------------------------------------------
def test_candidates_round_trip_and_order(store):
    campaign = store.create_campaign(_brief())
    c0 = _candidate(campaign.id, 0, "first-idea")
    c1 = _candidate(campaign.id, 1, "second-idea")
    # Insert out of order; list must still be queue-ordered.
    store.save_candidate(c1)
    store.save_candidate(c0)

    listed = store.list_candidates(campaign.id)
    assert [c.queue_index for c in listed] == [0, 1]
    assert listed[0].slug == "first-idea"


def test_candidate_status_update_persists(store):
    campaign = store.create_campaign(_brief())
    cand = _candidate(campaign.id, 0, "doomed-idea")
    store.save_candidate(cand)

    cand.status = CandidateStatus.DEAD
    store.save_candidate(cand)

    fetched = store.get_candidate(cand.id)
    assert fetched.status == CandidateStatus.DEAD


# --- checkpoints + crash recovery (HM-Pro Lesson 1) ----------------------
def test_checkpoint_stores_full_context(store):
    campaign = store.create_campaign(_brief())
    # Lesson 1: verify_findings MUST survive into the checkpoint.
    ctx = {
        "stage": "VERIFY",
        "design": {"method": "split-and-merge"},
        "verify_findings": [
            {"kind": "counterexample", "detail": "fails for n=2"},
        ],
    }
    cp = store.save_checkpoint(campaign.id, "VERIFY", ctx, candidate_id="c-1")
    assert cp.seq >= 1
    assert cp.context["verify_findings"][0]["detail"] == "fails for n=2"

    latest = store.latest_checkpoint(campaign.id)
    assert latest is not None
    assert latest.stage == "VERIFY"
    assert latest.context == ctx


def test_latest_checkpoint_is_most_recent(store):
    campaign = store.create_campaign(_brief())
    store.save_checkpoint(campaign.id, "DESIGN", {"round": 1})
    store.save_checkpoint(campaign.id, "VERIFY", {"round": 1})
    latest = store.latest_checkpoint(campaign.id)
    assert latest.stage == "VERIFY"


# --- restore after "crash" ------------------------------------------------
def test_restore_recovers_full_snapshot(store):
    campaign = store.create_campaign(_brief())
    campaign.status = CampaignStatus.VERIFYING
    campaign.current_candidate_id = "c-active"
    store.save_campaign(campaign)
    store.save_candidate(_candidate(campaign.id, 0, "lead-idea"))
    store.save_candidate(_candidate(campaign.id, 1, "backup-idea"))
    store.save_checkpoint(
        campaign.id, "VERIFY", {"verify_findings": ["obligation A"]}, "c-active"
    )

    snap = store.restore(campaign.id)
    assert snap is not None
    assert snap.campaign.status == CampaignStatus.VERIFYING
    assert snap.campaign.current_candidate_id == "c-active"
    assert len(snap.candidates) == 2
    assert snap.checkpoint.context["verify_findings"] == ["obligation A"]


def test_crash_recovery_across_new_connection(tmp_path):
    """Simulate a crash: close the process, reopen the DB, and resume.

    This is the core crash-recovery guarantee — the data must survive a hard
    restart and the latest checkpoint must carry the working context.
    """
    db = tmp_path / "haa.db"
    campaign_id = None
    with StateStore(db) as s:
        campaign = s.create_campaign(_brief())
        campaign_id = campaign.id
        campaign.status = CampaignStatus.VERIFYING
        s.commit_transition(
            campaign,
            stage="VERIFY",
            context={"verify_findings": ["counterexample X"], "round": 2},
        )

    # "Crash": brand-new connection to the same file.
    with StateStore(db) as s2:
        snap = s2.restore(campaign_id)
    assert snap is not None
    assert snap.campaign.status == CampaignStatus.VERIFYING
    assert snap.has_checkpoint
    # Lesson 1 guarantee: the verify context is intact after the crash.
    assert snap.checkpoint.context["verify_findings"] == ["counterexample X"]
    assert snap.checkpoint.stage == "VERIFY"


def test_commit_transition_is_atomic(store, monkeypatch):
    """A failing transition rolls back BOTH campaign update and checkpoint."""
    campaign = store.create_campaign(_brief())
    assert store.latest_checkpoint(campaign.id) is None

    # Force save_checkpoint to fail after the campaign row has been written
    # inside the same transaction.
    def boom(*a, **k):
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr(store, "save_checkpoint", boom)

    campaign.status = CampaignStatus.GRADING
    with pytest.raises(RuntimeError):
        store.commit_transition(campaign, stage="GRADE", context={"x": 1})

    # Neither the status change nor a new checkpoint should have persisted.
    fetched = store.get_campaign(campaign.id)
    assert fetched.status == CampaignStatus.QUEUED
    assert store.latest_checkpoint(campaign.id) is None


# --- stats ----------------------------------------------------------------
def test_stats_aggregates(store):
    store.create_campaign(_brief())
    campaign = store.create_campaign(Brief(title="Other", problem_area="pb"))
    campaign.status = CampaignStatus.PUBLISHED
    campaign.budget_used = 2.5
    store.save_campaign(campaign)

    stats = store.stats()
    assert stats["total_campaigns"] == 2
    assert stats["published"] == 1
    assert stats["total_budget_used"] == pytest.approx(2.5)
    assert stats["by_status"]["published"] == 1
