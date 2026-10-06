"""Campaign — one run of the pipeline against a single Brief.

A Campaign owns the budget for its lifetime, tracks which candidate is
currently being worked on, and carries a lifecycle ``status``. The status is
*finer-grained than* the pipeline's ``StageName`` (see haa/pipeline.py): the
status includes bookkeeping states (QUEUED, RETIRED) that are not discrete
stages. The pipeline translates between the two.

Core fields (per spec): id, brief_hash, status, created_at, budget_limit,
budget_used. ``updated_at`` and ``current_candidate_id`` are practical extras
needed to drive the state machine and the candidate queue (HM-Pro Lesson 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CampaignStatus(str, Enum):
    """Lifecycle state of a campaign.

    The -ING values mirror the pipeline stage currently running; QUEUED is the
    initial state and PUBLISHED/RETIRED are terminal. RETIRED covers budget
    exhaustion and queue-exhaustion termination.
    """

    QUEUED = "queued"  # brief accepted, waiting to start SEEK
    SEEKING = "seeking"
    NOVELTY_CHECK = "novelty_check"
    SCREENING = "screening"
    DESIGNING = "designing"
    VERIFYING = "verifying"
    GRADING = "grading"
    WRITING = "writing"
    REVIEWING = "reviewing"
    REFINING = "refining"
    EXP_SPECIFYING = "exp_specifying"  # 实验规格设计中（Phase A 新增）
    EXP_CHECKING = "exp_checking"      # 实验可行性检验中
    # Part I 产出（含实验设计）已就绪，等待人工审核通过后再投入 Part II
    # （WRITE 及之后的真实实验）。pipeline 在 HUMAN_REVIEW 阶段暂停于此状态。
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    PUBLISHED = "published"  # shipped successfully
    RETIRED = "retired"  # terminated (budget / queue exhausted / dead)


def utcnow() -> datetime:
    """Timezone-aware UTC 'now'. Centralised so tests can monkeypatch it."""
    return datetime.now(timezone.utc)


class Campaign(BaseModel):
    """A single pipeline run.

    Budget is tracked here (per-campaign cap + spent). The *global* cap lives
    in the BudgetManager (haa/budget.py) since it spans campaigns.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    brief_hash: str = Field(..., description="SHA-256 of the seeding Brief.")
    title: str = Field(default="", description="Denormalised brief title for display.")
    status: CampaignStatus = Field(default=CampaignStatus.QUEUED)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # --- Budget (USD) -------------------------------------------------------
    budget_limit: float = Field(default=20.0, ge=0, description="Per-campaign cap (USD).")
    budget_used: float = Field(default=0.0, ge=0, description="Amount spent so far (USD).")

    # --- Candidate queue (HM-Pro Lesson 4) ---------------------------------
    # The candidate currently flowing through DESIGN→…→REFINE. None until SEEK
    # produces candidates and the pipeline selects the head of the queue.
    current_candidate_id: str | None = None

    def touch(self) -> None:
        """Bump updated_at on any mutation. Callers should invoke after edits."""
        self.updated_at = utcnow()

    @property
    def budget_remaining(self) -> float:
        """Remaining per-campaign budget; never negative."""
        return max(0.0, self.budget_limit - self.budget_used)

    @property
    def is_terminal(self) -> bool:
        """True once the campaign has reached a final state."""
        return self.status in (CampaignStatus.PUBLISHED, CampaignStatus.RETIRED)
