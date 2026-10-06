"""Smoke tests for the Project outer-framework models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from haa.models import (
    Brief,
    MoribundEntry,
    Phase,
    Precursor,
    Project,
    ProjectHyperparams,
    ProjectStatus,
    Track,
)


# --------------------------------------------------------------------------- #
#  Project
# --------------------------------------------------------------------------- #

def test_project_defaults():
    p = Project(brief=Brief(title="T", problem_area="AI", track=Track.THEORY))
    assert p.status == ProjectStatus.NOT_STARTED
    assert p.phase == Phase.PR
    assert not p.is_terminal
    assert not p.is_moribund
    assert p.precursors == []
    assert p.selected_precursor_campaign_id is None
    assert p.moribund_reason == ""
    assert p.moribund_history == []


def test_project_id_is_unique():
    a = Project(brief=Brief(title="A", problem_area="x", track=Track.THEORY))
    b = Project(brief=Brief(title="B", problem_area="y", track=Track.THEORY))
    assert a.id != b.id


def test_project_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Project(
            brief=Brief(title="T", problem_area="x", track=Track.THEORY),
            surprise="nope",  # type: ignore[call-arg]
        )


def test_project_is_terminal():
    base = Brief(title="T", problem_area="x", track=Track.THEORY)
    p = Project(brief=base)
    assert not p.is_terminal
    p.status = ProjectStatus.MORIBUND
    assert not p.is_terminal  # MORIBUND is NOT terminal (resumable)
    p.status = ProjectStatus.COMPLETED
    assert p.is_terminal
    p.status = ProjectStatus.ABORTED
    assert p.is_terminal


def test_project_is_moribund():
    p = Project(brief=Brief(title="T", problem_area="x", track=Track.THEORY))
    assert not p.is_moribund
    p.status = ProjectStatus.MORIBUND
    assert p.is_moribund


def test_project_touch_updates_timestamp():
    p = Project(brief=Brief(title="T", problem_area="x", track=Track.THEORY))
    old = p.updated_at
    p.touch()
    assert p.updated_at >= old


# --------------------------------------------------------------------------- #
#  ProjectHyperparams
# --------------------------------------------------------------------------- #

def test_hyperparams_defaults():
    hp = ProjectHyperparams()
    assert hp.seek_base_count == 5
    assert hp.output_candidate_count == 3
    assert hp.max_design_rounds == 3
    assert hp.auto_approve_human_review is True  # default True (user confirmed)
    # P2/P3 placeholders
    assert hp.beam_search_width == 3
    assert hp.pre_prompt == ""


def test_hyperparams_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ProjectHyperparams(seek_base_count=5, bogus=True)  # type: ignore[call-arg]


def test_hyperparams_validates_ranges():
    with pytest.raises(ValidationError):
        ProjectHyperparams(seek_base_count=0)  # ge=1
    with pytest.raises(ValidationError):
        ProjectHyperparams(output_candidate_count=0)  # ge=1


def test_hyperparams_auto_approve_can_be_disabled():
    hp = ProjectHyperparams(auto_approve_human_review=False)
    assert hp.auto_approve_human_review is False


# --------------------------------------------------------------------------- #
#  Precursor
# --------------------------------------------------------------------------- #

def test_precursor_minimal():
    pre = Precursor(
        campaign_id="camp1",
        candidate_id="cand1",
        candidate_slug="good-idea",
        candidate_title="A Good Idea",
    )
    assert pre.grade is None
    assert pre.paper == {}
    assert pre.review == {}
    assert pre.exp_spec == {}
    assert not pre.exp_degraded
    assert not pre.theory_only


def test_precursor_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Precursor(
            campaign_id="c", candidate_id="c", candidate_slug="s",
            candidate_title="t", bogus=True,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
#  MoribundEntry
# --------------------------------------------------------------------------- #

def test_moribund_entry():
    e = MoribundEntry(phase="p1", reason="p1_no_precursors", diagnostic="all ideas trivial")
    assert e.phase == "p1"
    assert e.diagnostic == "all ideas trivial"


def test_moribund_entry_rejects_extra_fields():
    with pytest.raises(ValidationError):
        MoribundEntry(phase="p1", reason="x", bogus=True)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
#  JSON round-trip (critical for StateStore persistence)
# --------------------------------------------------------------------------- #

def test_project_round_trips_through_json():
    """Project must survive model_dump_json → model_validate_json with all
    nested models (Brief, Hyperparams, Precursor, MoribundEntry) intact."""
    p = Project(
        brief=Brief(
            title="Test Project",
            problem_area="Generalization bounds",
            constraints=["must be constructive"],
            track=Track.THEORY,
        ),
        hyperparams=ProjectHyperparams(seek_base_count=10, output_candidate_count=4),
        status=ProjectStatus.IN_PROGRESS,
        phase=Phase.ARV,
    )
    p.precursors.append(Precursor(
        campaign_id="camp1", candidate_id="c1",
        candidate_slug="fast-bound", candidate_title="Fast Bound",
        grade="solid", paper={"abstract": "..."}, review={"overall": 8.0},
    ))
    p.moribund_history.append(MoribundEntry(
        phase="p1", reason="test", diagnostic="diagnostic text",
    ))

    json_str = p.model_dump_json()
    restored = Project.model_validate_json(json_str)

    assert restored.id == p.id
    assert restored.status == ProjectStatus.IN_PROGRESS
    assert restored.phase == Phase.ARV
    assert restored.brief.title == "Test Project"
    assert restored.brief.constraints == ["must be constructive"]
    assert restored.hyperparams.seek_base_count == 10
    assert restored.hyperparams.output_candidate_count == 4
    assert len(restored.precursors) == 1
    assert restored.precursors[0].candidate_slug == "fast-bound"
    assert restored.precursors[0].grade == "solid"
    assert len(restored.moribund_history) == 1
    assert restored.moribund_history[0].diagnostic == "diagnostic text"
