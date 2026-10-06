"""Candidate — one paper idea produced by SEEK.

SEEK emits 4-6 candidates per campaign. They form an ordered **queue**; the
pipeline works the head, and on a dead verdict (HM-Pro Lesson 4: trivial or
loophole, or a failed screen) advances to the next. A campaign terminates only
when the queue is exhausted.

Round counters live *here*, on the candidate, because each candidate has its
own DESIGN⇄VERIFY and REVIEW⇄REFINE attempts (HM-Pro: max 2-3 rounds each).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from haa.models.campaign import utcnow


class CandidateStatus(str, Enum):
    """Lifecycle of a single candidate within the queue."""

    PROPOSED = "proposed"  # fresh from SEEK, awaiting NOVELTY/SCREEN
    ACTIVE = "active"  # cleared novelty/screen, in design→…→refine flow
    DEAD = "dead"  # archived: duplicate / failed screen / trivial / loophole
    PUBLISHED = "published"  # shipped as the campaign's paper
    FILTERED = "filtered"  # cut by output_candidate_count cap after SEEK (P1 batch)


class GradeVerdict(str, Enum):
    """GRADE outcome (HM-Pro: 扎实/单薄/琐碎/钻空子).

    TRIVIAL and LOOPHOLE kill the candidate → advance the queue (Lesson 4).
    THIN is refinable; SOLID proceeds to WRITE.
    """

    SOLID = "solid"  # 扎实 — proceed to WRITE
    THIN = "thin"  # 单薄 — needs more substance (refinable)
    TRIVIAL = "trivial"  # 琐碎 — kill candidate, advance queue
    LOOPHOLE = "loophole"  # 钻空子 — kill candidate, advance queue


# Filesystem-safe slug: lowercase letters, digits, single dashes.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Candidate(BaseModel):
    """A candidate paper idea.

    significance / win_odds / difficulty are required (Lesson 6): SEEK must
    populate them so the queue can be ordered and the budget can reason about
    expected value. They are floats in [0, 1].
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    campaign_id: str = Field(..., description="Owning campaign.")
    slug: str = Field(
        ..., description="Filesystem-safe short id, e.g. 'fast-conv-upper-bound'."
    )
    title: str = Field(..., min_length=1)

    # SEEK's estimates — all required (Lesson 6).
    significance: float = Field(..., ge=0.0, le=1.0, description="Estimated impact.")
    win_odds: float = Field(..., ge=0.0, le=1.0, description="P(this works out).")
    difficulty: float = Field(..., ge=0.0, le=1.0, description="Effort/risk estimate.")

    # SEEK's structured spec (Phase 3) — optional, defaults empty so legacy
    # constructions still validate. These carry the paired claims (philosophy 4)
    # and rationale forward so DESIGN/VERIFY/GRADE/WRITE don't lose them.
    rationale: str = Field(default="", description="Why this idea is worth doing (from SEEK).")
    positive_claim: str = Field(default="", description="Positive claim: what we can achieve.")
    negative_claim: str = Field(default="", description="Negative claim: bound / impossibility / counterexample direction.")
    attack_plan: str = Field(default="", description="Planned attack + most likely failure point.")
    closest_prior_work: str = Field(default="", description="Nearest existing work found in dedup.")

    status: CandidateStatus = Field(default=CandidateStatus.PROPOSED)
    grade: GradeVerdict | None = Field(
        default=None, description="Set by GRADE; None until graded."
    )

    # --- Queue + round bookkeeping -----------------------------------------
    queue_index: int = Field(..., ge=0, description="Position in the campaign queue.")
    design_rounds: int = Field(default=0, ge=0, description="DESIGN⇄VERIFY rounds used.")
    review_rounds: int = Field(default=0, ge=0, description="REVIEW⇄REFINE rounds used.")

    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        """Slugs become directory names; enforce a safe shape."""
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must be lowercase letters/digits separated by single dashes "
                "(e.g. 'fast-conv-upper-bound')"
            )
        return v

    @property
    def is_dead(self) -> bool:
        """True if this candidate has been archived and should be skipped."""
        return self.status == CandidateStatus.DEAD

    @property
    def is_terminal(self) -> bool:
        """True once the candidate has shipped, been killed, or been filtered out."""
        return self.status in (
            CandidateStatus.DEAD,
            CandidateStatus.PUBLISHED,
            CandidateStatus.FILTERED,
        )
