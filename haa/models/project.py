"""Project — the outer-framework entity that owns a full research lifecycle.

A Project is the top-level entity in HAA's macro state machine
(PR→P1→ARV→P2→EA→P3). It manages multiple Campaigns across three phases and
carries the research Brief, per-project hyperparameters, harvested precursors,
and the MORIBUND/ABORTED lifecycle.

The macro state machine is driven by :class:`~haa.project_controller.ProjectController`;
this module only defines the data model. Project ↔ Campaign linkage is via the
``project_campaigns`` mapping table in :class:`~haa.state.StateStore` (NOT a
``project_id`` field on Campaign — Campaign is ``extra="forbid"``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from haa.models.brief import Brief
from haa.models.campaign import utcnow


class ProjectStatus(str, Enum):
    """Lifecycle state of a Project (the macro state machine).

    NOT_STARTED  — Brief submitted, awaiting the human switch (PR phase).
    IN_PROGRESS  — Pipeline running (P1/P2/P3 + human gates blocking).
    HOLD         — Waiting for user assistance (batch 16 P2-b; HOLD↔RUNNING
                   legal; distinct from MORIBUND: HOLD=等人, MORIBUND=等修).
    MORIBUND     — Agent refuses to advance; still resumable (Agent's highest
                   authority — only the user can ABORT). Narrowed (batch 18):
                   only code-exhaustion or budget-exhaustion enters MORIBUND.
    COMPLETED    — P3 done + human approved (terminal, user-only).
    ABORTED      — User killed it (terminal, user-only, irrecoverable).
    """

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    HOLD = "hold"
    MORIBUND = "moribund"
    COMPLETED = "completed"
    ABORTED = "aborted"


class Phase(str, Enum):
    """Where in the macro state machine the Project currently sits.

    Orthogonal to :class:`ProjectStatus`: a project that is IN_PROGRESS can be
    in any of P1/P2/P3; MORIBUND can occur in P1 or P2.
    """

    PR = "pr"      # Pre-Research (human writes the Brief)
    P1 = "p1"      # Phase I — precursor batch (automated)
    ARV = "arv"    # Artificial Re-Verification (human picks a precursor)
    P2 = "p2"      # Phase II — experiments (v0.7+)
    EA = "ea"      # Experiment Analysis (human; v0.7+)
    P3 = "p3"      # Phase III — final paper (v0.9+)
    DONE = "done"  # post-P3, awaiting human approval → COMPLETED


class ProjectHyperparams(BaseModel):
    """Per-project tunables.

    v0.4 only uses the P1 fields; P2/P3 fields are placeholders so the model
    is forward-compatible without migrations.
    """

    model_config = ConfigDict(extra="forbid")

    # --- P1 (v0.4) -------------------------------------------------------
    seek_base_count: int = Field(default=5, ge=1, le=100)
    output_candidate_count: int = Field(default=3, ge=1, le=20)
    max_design_rounds: int = Field(default=3, ge=1)
    max_exp_rounds: int = Field(default=3, ge=1)
    max_review_rounds: int = Field(default=3, ge=1)
    auto_approve_human_review: bool = Field(
        default=True,
        description="If True, ProjectController auto-approves each Campaign's "
        "HUMAN_REVIEW gate during P1 batch (the real paper-level review happens "
        "at ARV). Default True for autonomous batch runs.",
    )

    # --- P2 placeholders (v0.7+) -----------------------------------------
    beam_search_width: int = Field(default=3, ge=1)
    ssh_host: str = ""
    ssh_user: str = ""

    # --- P3 placeholders (v0.9+) -----------------------------------------
    pre_prompt: str = ""


class Precursor(BaseModel):
    """One paper precursor harvested from a PUBLISHED P1 Campaign.

    Carries everything ARV (human re-verification) needs to pick a survivor
    into P2: the paper draft sections, the GRADE verdict, the REVIEW scores,
    and the experiment design spec.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    candidate_id: str
    candidate_slug: str
    candidate_title: str
    grade: str | None = None  # "solid" / "thin" (verbatim from GradeVerdict.value)
    paper: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    exp_spec: dict[str, Any] = Field(default_factory=dict)
    exp_degraded: bool = False
    theory_only: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class MoribundEntry(BaseModel):
    """One MORIBUND event in the project's history.

    A project can go MORIBUND → recovered → MORIBUND multiple times; each
    cycle is recorded here so the recovery warning ("this experiment
    previously failed at X due to Y") can reference the last death.
    """

    model_config = ConfigDict(extra="forbid")

    phase: str
    reason: str
    diagnostic: str = ""
    at: datetime = Field(default_factory=utcnow)


class Project(BaseModel):
    """One research production lifecycle.

    Owns multiple Campaigns via the ``project_campaigns`` mapping table (NOT a
    ``project_id`` on Campaign). The Brief is embedded here as the single
    source of truth; individual Campaigns receive copies.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    brief: Brief
    status: ProjectStatus = ProjectStatus.NOT_STARTED
    phase: Phase = Phase.PR
    hyperparams: ProjectHyperparams = Field(default_factory=ProjectHyperparams)

    # P1 outputs — populated by run_p1_batch, consumed by ARV.
    precursors: list[Precursor] = Field(default_factory=list)
    selected_precursor_campaign_id: str | None = None  # set at ARV→P2
    # v1.0.2 ARV 多选：用户要求可勾选多个合格前体进入下一阶段。首个元素
    # 同步进 selected_precursor_campaign_id——下游 P2/P3 状态机按单前体
    # 消费（保持兼容），完整清单留在此字段。
    selected_precursor_campaign_ids: list[str] = Field(default_factory=list)

    # P2 outputs — populated by _run_p2_batch (v0.7).
    exp_code_dir: str = ""  # generated experiment code directory
    exp_results: dict[str, Any] = Field(default_factory=dict)  # metrics + analysis

    # P3 outputs — populated by _run_p3_batch (v0.9).
    p3_paper_dir: str = ""  # LaTeX paper directory
    p3_deliverable_path: str = ""  # final .zip path

    # MORIBUND state — current + full history for recovery warnings.
    moribund_reason: str = ""
    moribund_diagnostic: str = ""
    moribund_history: list[MoribundEntry] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        """Stamp updated_at (called by StateStore.save_project)."""
        self.updated_at = utcnow()

    @property
    def is_terminal(self) -> bool:
        """True once the project has been completed or aborted."""
        return self.status in (ProjectStatus.COMPLETED, ProjectStatus.ABORTED)

    @property
    def is_moribund(self) -> bool:
        """True if the project is currently in the MORIBUND (paused) state."""
        return self.status == ProjectStatus.MORIBUND
