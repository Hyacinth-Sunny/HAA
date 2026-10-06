"""Tests for the projects + project_campaigns tables in StateStore."""

from __future__ import annotations

import pytest

from haa.models import (
    Brief,
    Phase,
    Precursor,
    Project,
    ProjectHyperparams,
    ProjectStatus,
    Track,
)
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


def _brief(title: str = "T") -> Brief:
    return Brief(title=title, problem_area="AI", track=Track.THEORY)


def _project(title: str = "Test Project") -> Project:
    return Project(brief=_brief(title))


# --- save / get / list -------------------------------------------------------

def test_save_and_get_project(store):
    p = _project("Alpha")
    store.save_project(p)
    got = store.get_project(p.id)
    assert got is not None
    assert got.brief.title == "Alpha"
    assert got.status == ProjectStatus.NOT_STARTED
    assert got.phase == Phase.PR


def test_get_project_returns_none_for_missing(store):
    assert store.get_project("nonexistent") is None


def test_list_projects_ordered_by_creation(store):
    p1 = _project("First")
    store.save_project(p1)
    p2 = _project("Second")
    store.save_project(p2)
    p3 = _project("Third")
    store.save_project(p3)
    listed = store.list_projects()
    assert [p.brief.title for p in listed] == ["First", "Second", "Third"]


def test_save_project_upserts(store):
    p = _project()
    store.save_project(p)
    # Mutate and re-save — should update, not duplicate.
    p.status = ProjectStatus.IN_PROGRESS
    p.phase = Phase.P1
    store.save_project(p)
    listed = store.list_projects()
    assert len(listed) == 1
    assert listed[0].status == ProjectStatus.IN_PROGRESS
    assert listed[0].phase == Phase.P1


# --- link_campaign / list_campaigns_for_project ------------------------------

def test_link_campaign_and_list(store):
    p = _project()
    store.save_project(p)
    c1 = store.create_campaign(_brief())
    c2 = store.create_campaign(_brief())
    store.link_campaign(p.id, c1.id, "scout")
    store.link_campaign(p.id, c2.id, "worker")
    linked = store.list_campaigns_for_project(p.id)
    assert len(linked) == 2
    assert linked[0] == (c1.id, "scout")
    assert linked[1] == (c2.id, "worker")


def test_link_campaign_idempotent_updates_role(store):
    p = _project()
    store.save_project(p)
    c = store.create_campaign(_brief())
    store.link_campaign(p.id, c.id, "scout")
    store.link_campaign(p.id, c.id, "p2")  # re-link with different role
    linked = store.list_campaigns_for_project(p.id)
    assert len(linked) == 1  # no duplicate
    assert linked[0] == (c.id, "p2")  # role updated


def test_list_campaigns_for_project_empty(store):
    p = _project()
    store.save_project(p)
    assert store.list_campaigns_for_project(p.id) == []


def test_link_campaign_enforces_fk(store):
    """Linking a non-existent campaign must fail (FK constraint)."""
    p = _project()
    store.save_project(p)
    with pytest.raises(Exception):
        store.link_campaign(p.id, "nonexistent-campaign", "scout")


# --- complex project round-trip ---------------------------------------------

def test_project_with_precursors_round_trips(store):
    """A project with precursors + moribund history survives the store."""
    p = Project(
        brief=Brief(title="Complex", problem_area="X", constraints=["c1"], track=Track.THEORY),
        hyperparams=ProjectHyperparams(seek_base_count=8, output_candidate_count=2),
        status=ProjectStatus.IN_PROGRESS,
        phase=Phase.ARV,
    )
    p.precursors.append(Precursor(
        campaign_id="camp1", candidate_id="c1", candidate_slug="idea-a",
        candidate_title="Idea A", grade="solid",
        paper={"abstract": "We show..."}, review={"overall": 8.5},
    ))
    store.save_project(p)
    got = store.get_project(p.id)
    assert got is not None
    assert got.hyperparams.seek_base_count == 8
    assert got.hyperparams.output_candidate_count == 2
    assert len(got.precursors) == 1
    assert got.precursors[0].candidate_slug == "idea-a"
    assert got.precursors[0].grade == "solid"
    assert got.precursors[0].paper["abstract"] == "We show..."


# --- crash recovery ----------------------------------------------------------

def test_project_crash_recovery_across_new_connection(tmp_path):
    """A project must survive a hard restart (new DB connection)."""
    db = tmp_path / "haa.db"
    project_id = None
    with StateStore(db) as s:
        p = _project("Survivor")
        p.status = ProjectStatus.MORIBUND
        p.phase = Phase.P1
        p.moribund_reason = "p1_no_precursors"
        p.moribund_diagnostic = "All ideas were trivial."
        s.save_project(p)
        project_id = p.id

    # "Crash": brand-new connection to the same file.
    with StateStore(db) as s2:
        got = s2.get_project(project_id)
    assert got is not None
    assert got.brief.title == "Survivor"
    assert got.status == ProjectStatus.MORIBUND
    assert got.moribund_reason == "p1_no_precursors"
    assert got.moribund_diagnostic == "All ideas were trivial."
