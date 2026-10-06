"""Tests for the dual-layer budget manager (Lesson 5 + crash-safety)."""

import pytest

from haa.budget import (
    BudgetExhausted,
    BudgetManager,
    Reservation,
    is_gated_stage,
)
from haa.models import Brief
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def budget(store):
    return BudgetManager(store, global_limit=10.0)


def _make_campaign(store, budget_limit=5.0, used=0.0):
    c = store.create_campaign(Brief(title="T", problem_area="P"), budget_limit=budget_limit)
    if used:
        c.budget_used = used
        store.save_campaign(c)
    return c


# --- gating classification (Lesson 5) -------------------------------------
def test_grade_and_write_are_ungated_everything_else_gated():
    assert is_gated_stage("SEEK") is True
    assert is_gated_stage("SCREEN") is True
    assert is_gated_stage("DESIGN") is True
    assert is_gated_stage("VERIFY") is True
    assert is_gated_stage("REVIEW") is True
    assert is_gated_stage("REFINE") is True
    # The harvest stages are explicitly exempt.
    assert is_gated_stage("GRADE") is False
    assert is_gated_stage("WRITE") is False


# --- dual-layer deduction --------------------------------------------------
def test_pre_spend_deducts_from_campaign_and_global(budget, store):
    c = _make_campaign(store, budget_limit=5.0)
    assert budget.campaign_remaining(c.id) == pytest.approx(5.0)
    assert budget.global_remaining() == pytest.approx(10.0)

    budget.pre_spend(c.id, 2.0, gate=True)
    assert budget.campaign_remaining(c.id) == pytest.approx(3.0)
    # Global is the sum of all campaigns; this one now uses 2.0.
    assert budget.global_remaining() == pytest.approx(8.0)


def test_pre_spend_across_two_campaigns_consumes_global(budget, store):
    a = _make_campaign(store, budget_limit=5.0)
    b = _make_campaign(store, budget_limit=5.0)
    # global limit is 10; spend 7 across the two.
    budget.pre_spend(a.id, 4.0, gate=True)
    budget.pre_spend(b.id, 3.0, gate=True)
    assert budget.global_remaining() == pytest.approx(3.0)


# --- campaign-cap gating --------------------------------------------------
def test_gated_spend_raises_when_campaign_broke(budget, store):
    c = _make_campaign(store, budget_limit=2.0)
    budget.pre_spend(c.id, 2.0, gate=True)  # exactly exhausts
    with pytest.raises(BudgetExhausted) as exc:
        budget.pre_spend(c.id, 0.5, gate=True)
    assert exc.value.scope == "campaign"
    assert exc.value.campaign_id == c.id


# --- global-cap gating ----------------------------------------------------
def test_gated_spend_raises_when_global_broke(budget, store):
    # Each campaign has a generous limit (15) so the campaign gate never binds;
    # the global cap (10) is what should trip. Two campaigns exist to show the
    # global limit spans campaigns.
    a = _make_campaign(store, budget_limit=15.0)
    _make_campaign(store, budget_limit=15.0)
    budget.pre_spend(a.id, 9.0, gate=True)  # 9 of 10 global used
    with pytest.raises(BudgetExhausted) as exc:
        budget.pre_spend(a.id, 2.0, gate=True)  # would push global to 11
    assert exc.value.scope == "global"


# --- Lesson 5: harvest stages are NEVER gated -----------------------------
def test_ungated_spend_proceeds_even_when_over_budget(budget, store):
    """GRADE/WRITE must run even if both caps are exhausted."""
    c = _make_campaign(store, budget_limit=1.0)
    budget.pre_spend(c.id, 1.0, gate=True)  # campaign now at its 1.0 cap

    # A harvest call (gate=False) must NOT raise, despite being over budget.
    res = budget.pre_spend(c.id, 0.75, gate=False)
    assert isinstance(res, Reservation)
    assert res.gate is False
    # budget_used is allowed to exceed budget_limit for harvest calls.
    fetched = store.get_campaign(c.id)
    assert fetched.budget_used == pytest.approx(1.75)


def test_ungated_spend_proceeds_when_global_broke(budget, store):
    a = _make_campaign(store, budget_limit=10.0)
    budget.pre_spend(a.id, 10.0, gate=True)  # global (10) now exhausted
    # GRADE/WRITE still allowed.
    res = budget.pre_spend(a.id, 0.5, gate=False)
    assert res.gate is False


# --- crash recovery (persist before call) ---------------------------------
def test_pre_spend_survives_crash(tmp_path):
    """The deduction is in SQLite before the call; a fresh manager sees it."""
    db = tmp_path / "haa.db"
    cid = None
    with StateStore(db) as s:
        c = s.create_campaign(Brief(title="T", problem_area="P"), budget_limit=5.0)
        cid = c.id
        bm = BudgetManager(s, global_limit=10.0)
        bm.pre_spend(cid, 3.0, gate=True)
        # "crash" — connection closes without record()

    with StateStore(db) as s2:
        bm2 = BudgetManager(s2, global_limit=10.0)
        # The 3.0 reserve survived the crash.
        assert bm2.campaign_remaining(cid) == pytest.approx(2.0)
        assert bm2.global_remaining() == pytest.approx(7.0)


# --- record() true-up -----------------------------------------------------
def test_record_true_up_when_actual_lower(budget, store):
    c = _make_campaign(store, budget_limit=5.0)
    res = budget.pre_spend(c.id, 2.0, gate=True)
    budget.record(res, 1.25)  # actually cheaper
    fetched = store.get_campaign(c.id)
    assert fetched.budget_used == pytest.approx(1.25)
    assert res.settled is True


def test_record_true_up_when_actual_higher(budget, store):
    c = _make_campaign(store, budget_limit=5.0)
    res = budget.pre_spend(c.id, 1.0, gate=True)
    budget.record(res, 2.5)  # more expensive than reserved
    fetched = store.get_campaign(c.id)
    assert fetched.budget_used == pytest.approx(2.5)


def test_record_refunds_on_zero_cost(budget, store):
    """A failed call (0 tokens) refunds the entire reserve."""
    c = _make_campaign(store, budget_limit=5.0)
    res = budget.pre_spend(c.id, 2.0, gate=True)
    budget.record(res, 0.0)
    assert store.get_campaign(c.id).budget_used == pytest.approx(0.0)


def test_record_cannot_settle_twice(budget, store):
    c = _make_campaign(store, budget_limit=5.0)
    res = budget.pre_spend(c.id, 1.0, gate=True)
    budget.record(res, 1.0)
    with pytest.raises(ValueError):
        budget.record(res, 1.0)


# --- summary --------------------------------------------------------------
def test_summary(budget, store):
    a = _make_campaign(store, budget_limit=5.0)
    budget.pre_spend(a.id, 2.0, gate=True)
    s = budget.summary()
    assert s["global_limit"] == 10.0
    assert s["global_used"] == pytest.approx(2.0)
    assert s["global_remaining"] == pytest.approx(8.0)
