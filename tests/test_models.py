"""Smoke tests for the pydantic data models (Brief, Campaign, Candidate)."""

import pytest
from pydantic import ValidationError

from haa.models import Brief, Campaign, Candidate, CampaignStatus, CandidateStatus, GradeVerdict, Track


def test_brief_minimal_and_hash_stability():
    b1 = Brief(title="Fast conv", problem_area="convolution upper bounds")
    b2 = Brief(title="Fast conv", problem_area="convolution upper bounds")
    assert b1.brief_hash() == b2.brief_hash()
    assert Track.THEORY == b1.track


def test_brief_hash_ignores_list_order_and_whitespace():
    a = Brief(title="X", problem_area="Y", constraints=["b", "a"], exclusions=[" z "])
    b = Brief(title="X", problem_area="Y", constraints=["a", "b"], exclusions=["z"])
    assert a.brief_hash() == b.brief_hash()


def test_brief_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Brief(title="X", problem_area="Y", surprise="nope")  # type: ignore[call-arg]


def test_campaign_defaults_and_budget_math():
    c = Campaign(brief_hash="abc")
    assert c.status == CampaignStatus.QUEUED
    assert c.budget_remaining == c.budget_limit
    assert not c.is_terminal
    c.budget_used = 5.0
    assert c.budget_remaining == pytest.approx(c.budget_limit - 5.0)


def test_campaign_id_is_unique():
    a = Campaign(brief_hash="a")
    b = Campaign(brief_hash="a")
    assert a.id != b.id


def test_candidate_requires_estimates():
    # significance/win_odds/difficulty are required (Lesson 6).
    with pytest.raises(ValidationError):
        Candidate(campaign_id="c", slug="ok-idea", title="T", queue_index=0)  # type: ignore[call-arg]


def test_candidate_bad_slug_rejected():
    with pytest.raises(ValidationError):
        Candidate(
            campaign_id="c",
            slug="Bad Slug!",
            title="T",
            significance=0.5,
            win_odds=0.5,
            difficulty=0.5,
            queue_index=0,
        )


def test_candidate_grade_lifecycle():
    cand = Candidate(
        campaign_id="c",
        slug="solid-idea",
        title="T",
        significance=0.8,
        win_odds=0.6,
        difficulty=0.4,
        queue_index=0,
    )
    assert cand.status == CandidateStatus.PROPOSED
    assert cand.grade is None
    cand.grade = GradeVerdict.TRIVIAL
    cand.status = CandidateStatus.DEAD
    assert cand.is_dead and cand.is_terminal
