"""Dual-layer, crash-safe budget manager.

Two limits are enforced:

* **per-campaign** — ``Campaign.budget_limit`` vs ``Campaign.budget_used``.
* **global** — a single cap across *all* campaigns, computed as the sum of every
  campaign's ``budget_used`` (so it is always consistent with the persisted
  per-campaign figures — no separate counter to drift).

Spending protocol (matches ``haa/llm/client.py``)::

    reservation = budget.pre_spend(campaign_id, estimate, gate=is_gated_stage(stage))
    try:
        resp = llm.call(...)
    finally:
        budget.record(reservation, actual_cost_from_tokens)

Two guarantees:

1. **Pre-deduct + persist before the call (crash-safe).** The estimated amount
   is added to ``budget_used`` and flushed to SQLite *before* the HTTP request
   goes out. If the process is killed mid-call, the money is still counted as
   spent — we assume the call may have succeeded. We never under-count.
2. **Gate repeatable loops only (HM-Pro Lesson 5).** ``pre_spend(..., gate=True)``
   raises :class:`BudgetExhausted` when the spend would breach a limit, which
   the pipeline uses to stop a loop. GRADE and WRITE pass ``gate=False`` — they
   are the "harvest" calls that finalize invested work and must NEVER be blocked
   (HM-Pro once burned 10 successful calls by gating the grading call).
"""

from __future__ import annotations

from dataclasses import dataclass

from haa.models import CampaignStatus
from haa.state import StateStore

# HM-Pro Lesson 5: only the harvest stages are exempt from the budget gate.
# Every other stage (incl. the repeatable SEEK/SCREEN, DESIGN/VERIFY,
# REVIEW/REFINE loops) is gated. P1_DIAGNOSTIC is ungated because the project
# is already MORIBUND — suppressing the diagnostic would hide the cause of
# death from the user.
UNGATED_STAGES: frozenset[str] = frozenset({
    "GRADE", "WRITE", "HUMAN_REVIEW",
    "P1_DIAGNOSTIC", "P2_ANALYZE", "P2_DIAGNOSTIC",
    "P3_PAPER_INTEGRATE", "P3_DIAGNOSTIC",
})


def is_gated_stage(stage: str) -> bool:
    """True for stages whose LLM calls may be blocked by the budget gate.

    GRADE and WRITE are ungated (they convert already-invested work into a
    grade / a paper and must always run). Everything else is gated.
    """
    return stage.upper() not in UNGATED_STAGES


class BudgetExhausted(Exception):
    """Raised when a *gated* spend would breach the per-campaign or global cap.

    ``scope`` is ``"campaign"`` or ``"global"`` so the pipeline can decide
    whether to retire just this campaign or stop globally.
    """

    def __init__(self, scope: str, limit: float, would_use: float, campaign_id: str | None):
        self.scope = scope
        self.limit = limit
        self.would_use = would_use
        self.campaign_id = campaign_id
        super().__init__(
            f"{scope} budget exhausted (campaign={campaign_id}): "
            f"would use {would_use:.4f} > limit {limit:.4f}"
        )


@dataclass
class Reservation:
    """Handle returned by ``pre_spend`` and consumed by ``record``.

    Carries the pre-deducted ``reserved`` amount so ``record`` can true it up to
    the real cost. ``settled`` guards against double-settling.
    """

    campaign_id: str
    reserved: float
    gate: bool
    settled: bool = False


class BudgetManager:
    """Accounting + gating for per-campaign and global spend."""

    def __init__(self, store: StateStore, global_limit: float = 500.0):
        self.store = store
        self.global_limit = float(global_limit)

    # -- read-only queries -------------------------------------------------
    def _global_used(self) -> float:
        """Global spend = sum of every campaign's persisted budget_used."""
        return sum(c.budget_used for c in self.store.list_campaigns())

    def global_remaining(self) -> float:
        return max(0.0, self.global_limit - self._global_used())

    def campaign_remaining(self, campaign_id: str) -> float:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {campaign_id}")
        return campaign.budget_remaining

    # -- spend protocol ----------------------------------------------------
    def pre_spend(
        self,
        campaign_id: str,
        amount: float,
        *,
        gate: bool = True,
    ) -> Reservation:
        """Reserve ``amount`` for an upcoming LLM call.

        * ``gate=True``  — enforce limits; raise :class:`BudgetExhausted` if the
          spend would breach the per-campaign or global cap.
        * ``gate=False`` — record the spend but NEVER raise (GRADE/WRITE,
          Lesson 5). The campaign may go over its cap; that is intended.

        Either way the deduction is **persisted to SQLite before returning**, so
        a crash after this point still counts the spend.
        """
        if amount < 0:
            raise ValueError("pre_spend amount must be non-negative")

        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {campaign_id}")

        camp_remaining = campaign.budget_remaining
        glob_remaining = self.global_remaining()

        if gate:
            would_use_campaign = campaign.budget_used + amount
            if amount > camp_remaining + 1e-9:
                raise BudgetExhausted(
                    "campaign", campaign.budget_limit, would_use_campaign, campaign_id
                )
            would_use_global = self._global_used() + amount
            if amount > glob_remaining + 1e-9:
                raise BudgetExhausted(
                    "global", self.global_limit, would_use_global, campaign_id
                )

        # Crash-safe reserve: persist the deduction BEFORE the call goes out.
        campaign.budget_used += amount
        self.store.save_campaign(campaign)
        return Reservation(campaign_id=campaign_id, reserved=amount, gate=gate)

    def record(self, reservation: Reservation, actual_cost: float) -> None:
        """True-up the reserved amount to the actual cost after the call.

        ``actual_cost`` typically comes from the response's token usage. If the
        call failed and produced no tokens, pass 0 and the reserve is refunded.

        Note: we do NOT re-gate here. The call already happened; we can only
        reconcile the books. The *next* gated pre_spend will see the updated
        ``budget_used`` and gate accordingly.
        """
        if reservation.settled:
            raise ValueError("reservation already settled")
        if actual_cost < 0:
            raise ValueError("actual_cost must be non-negative")

        campaign = self.store.get_campaign(reservation.campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {reservation.campaign_id}")

        delta = actual_cost - reservation.reserved
        campaign.budget_used = max(0.0, campaign.budget_used + delta)
        self.store.save_campaign(campaign)
        reservation.settled = True

    # -- status helpers ----------------------------------------------------
    def is_campaign_broke(self, campaign_id: str) -> bool:
        """True when the campaign has no remaining budget for gated loops."""
        return self.campaign_remaining(campaign_id) <= 1e-9

    def summary(self) -> dict[str, float]:
        return {
            "global_limit": self.global_limit,
            "global_used": round(self._global_used(), 6),
            "global_remaining": round(self.global_remaining(), 6),
        }


def terminate_on_exhaustion(scope: str, campaign_id: str | None) -> str:
    """Helper: decide a campaign's terminal status given the exhaustion scope.

    ``global`` exhaustion retires the campaign outright; ``campaign`` exhaustion
    also retires (no budget left for this campaign's loops). The pipeline uses
    this when catching :class:`BudgetExhausted`. Returns the CampaignStatus
    *value* so callers don't need to import the enum here.
    """
    _ = scope, campaign_id  # both scopes currently retire; kept for future nuance
    return CampaignStatus.RETIRED.value
