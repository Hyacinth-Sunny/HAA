"""SQLite-backed state store with crash-recovery checkpoints.

Responsibilities
----------------
* Persist ``Campaign`` and ``Candidate`` records (JSON columns + a few indexed
  columns for fast querying).
* Maintain an append-only ``checkpoints`` log. After **every** state
  transition the pipeline writes a checkpoint capturing the full working
  context (including ``verify_findings`` — HM-Pro Lesson 1: rework must not
  leave the next stage blind).
* On restart, ``restore()`` returns the latest campaign state, its candidate
  queue, and the most recent checkpoint so the pipeline can resume exactly
  where it stopped.

Concurrency
-----------
SQLite is opened with ``check_same_thread=False`` (the FastAPI server calls in
from a threadpool) and a process-local ``Lock`` serialises writes. WAL journal
mode gives crash-durability without blocking readers.

The store is synchronous on purpose: it is trivially unit-testable and the
fastapi layer can dispatch it to a threadpool. The LLM client/pipeline are the
async parts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from haa.models import Brief, Campaign, CampaignStatus, Candidate, Project


class Checkpoint(BaseModel):
    """One row in the append-only transition log.

    ``context`` is an opaque JSON blob owned by the pipeline — it carries
    whatever the running stage produced (design artifact, verify_findings,
    review scores, best snapshot, …). It is the crash-recovery payload.
    """

    seq: int
    campaign_id: str
    stage: str
    candidate_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ResumeSnapshot(BaseModel):
    """Everything the pipeline needs to resume a campaign after a restart."""

    campaign: Campaign
    candidates: list[Candidate] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None

    @property
    def has_checkpoint(self) -> bool:
        return self.checkpoint is not None


class Event(BaseModel):
    """One row in the append-only observability event log (Phase 6a-fronted).

    Where checkpoints carry business state, events carry the *measurement*
    stream — per-LLM-call cost/tokens/duration, tool calls, stage transitions,
    truncation signals. This is the data ``analyze_campaign`` aggregates for
    Phase 5 tuning (e.g. per-stage cost, 3σ outlier detection).
    """

    seq: int
    campaign_id: str | None = None
    stage: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    duration_s: float = 0.0
    created_at: datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    brief_hash  TEXT NOT NULL,
    status      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL,          -- full Campaign JSON
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL,
    queue_index  INTEGER NOT NULL,
    status       TEXT NOT NULL,
    data         TEXT NOT NULL,         -- full Candidate JSON
    created_at   TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_candidates_campaign
    ON candidates(campaign_id, queue_index);

CREATE TABLE IF NOT EXISTS checkpoints (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    stage        TEXT NOT NULL,
    candidate_id TEXT,
    context      TEXT NOT NULL,         -- opaque pipeline context JSON (Lesson 1)
    created_at   TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_campaign
    ON checkpoints(campaign_id, seq DESC);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT,                   -- nullable: global events have no campaign
    stage        TEXT,
    event_type   TEXT NOT NULL,          -- llm_call | tool_call | stage_start | stage_done | stage_truncated | ...
    payload      TEXT,                   -- opaque JSON
    cost_usd     REAL,
    tokens       INTEGER,
    duration_s   REAL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_campaign
    ON events(campaign_id, seq);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    phase       TEXT NOT NULL,
    data        TEXT NOT NULL,          -- full Project JSON (含嵌套 Brief/Hyperparams/Precursor)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_campaigns (
    project_id  TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    role        TEXT NOT NULL,          -- 'scout' | 'worker' | 'p2' | 'p3'
    created_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, campaign_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_campaigns_project
    ON project_campaigns(project_id);
"""


class StateStore:
    """SQLite persistence for campaigns, candidates, and checkpoints."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        # Ensure the data dir exists (e.g. data/haa.db → mkdir data/).
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage txns explicitly
        )
        self._conn.row_factory = sqlite3.Row
        # WAL = durable + concurrent readers; safe even if the process is killed.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- campaigns ---------------------------------------------------------
    def create_campaign(self, brief: Brief, budget_limit: float = 20.0) -> Campaign:
        """Build a Campaign from a Brief and persist it (status=QUEUED)."""
        campaign = Campaign(
            brief_hash=brief.brief_hash(),
            title=brief.title,
            budget_limit=budget_limit,
        )
        self.save_campaign(campaign)
        return campaign

    def save_campaign(self, campaign: Campaign) -> None:
        """Upsert a campaign row. Idempotent on id."""
        campaign.touch()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO campaigns (id, brief_hash, status, title, data, created_at, updated_at)
                VALUES (:id, :brief_hash, :status, :title, :data, :created_at, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    brief_hash = excluded.brief_hash,
                    status     = excluded.status,
                    title      = excluded.title,
                    data       = excluded.data,
                    updated_at = excluded.updated_at
                """,
                {
                    "id": campaign.id,
                    "brief_hash": campaign.brief_hash,
                    "status": campaign.status.value,
                    "title": campaign.title,
                    "data": campaign.model_dump_json(),
                    "created_at": campaign.created_at.isoformat(),
                    "updated_at": campaign.updated_at.isoformat(),
                },
            )

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        return Campaign.model_validate_json(row["data"]) if row else None

    def list_campaigns(self) -> list[Campaign]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM campaigns ORDER BY created_at ASC"
            ).fetchall()
        return [Campaign.model_validate_json(r["data"]) for r in rows]

    # -- projects ----------------------------------------------------------
    def save_project(self, project: Project) -> None:
        """Upsert a project row. Idempotent on id."""
        project.touch()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO projects (id, status, phase, data, created_at, updated_at)
                VALUES (:id, :status, :phase, :data, :created_at, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    status     = excluded.status,
                    phase      = excluded.phase,
                    data       = excluded.data,
                    updated_at = excluded.updated_at
                """,
                {
                    "id": project.id,
                    "status": project.status.value,
                    "phase": project.phase.value,
                    "data": project.model_dump_json(),
                    "created_at": project.created_at.isoformat(),
                    "updated_at": project.updated_at.isoformat(),
                },
            )

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return Project.model_validate_json(row["data"]) if row else None

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM projects ORDER BY created_at ASC"
            ).fetchall()
        return [Project.model_validate_json(r["data"]) for r in rows]

    def link_campaign(self, project_id: str, campaign_id: str, role: str) -> None:
        """Idempotently link a Campaign to a Project with a role.

        ``role`` is a free-form string: 'scout' / 'worker' for P1 batch,
        'p2' / 'p3' for later phases.
        """
        created_at = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO project_campaigns (project_id, campaign_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, campaign_id) DO UPDATE SET role = excluded.role
                """,
                (project_id, campaign_id, role, created_at),
            )

    def list_campaigns_for_project(self, project_id: str) -> list[tuple[str, str]]:
        """Return [(campaign_id, role)] for a project, ordered by creation."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT campaign_id, role FROM project_campaigns "
                "WHERE project_id = ? ORDER BY created_at ASC",
                (project_id,),
            ).fetchall()
        return [(r["campaign_id"], r["role"]) for r in rows]

    def delete_project(self, project_id: str) -> bool:
        """Delete a project and all linked data (campaigns, candidates,
        checkpoints, events). Returns True if the project existed.

        Only call this for terminal projects (COMPLETED / ABORTED).
        The project ID becomes recyclable (the UUID hex slot is freed).
        """
        linked = self.list_campaigns_for_project(project_id)
        with self._lock:
            # Check existence.
            exists = self._conn.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if exists is None:
                return False
            # Delete linked campaigns (FK cascades to candidates + checkpoints).
            for cid, _ in linked:
                self._conn.execute(
                    "DELETE FROM candidates WHERE campaign_id = ?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM checkpoints WHERE campaign_id = ?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM events WHERE campaign_id = ?", (cid,)
                )
                self._conn.execute(
                    "DELETE FROM campaigns WHERE id = ?", (cid,)
                )
            # Delete project (FK cascades to project_campaigns).
            self._conn.execute(
                "DELETE FROM project_campaigns WHERE project_id = ?", (project_id,)
            )
            self._conn.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
        return True

    # -- candidates --------------------------------------------------------
    def save_candidate(self, candidate: Candidate) -> None:
        """Upsert a candidate row."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO candidates (id, campaign_id, queue_index, status, data, created_at)
                VALUES (:id, :campaign_id, :queue_index, :status, :data, :created_at)
                ON CONFLICT(id) DO UPDATE SET
                    queue_index = excluded.queue_index,
                    status      = excluded.status,
                    data        = excluded.data
                """,
                {
                    "id": candidate.id,
                    "campaign_id": candidate.campaign_id,
                    "queue_index": candidate.queue_index,
                    "status": candidate.status.value,
                    "data": candidate.model_dump_json(),
                    "created_at": candidate.created_at.isoformat(),
                },
            )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return Candidate.model_validate_json(row["data"]) if row else None

    def list_candidates(self, campaign_id: str) -> list[Candidate]:
        """Candidates for a campaign, ordered by queue position."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM candidates WHERE campaign_id = ? ORDER BY queue_index ASC",
                (campaign_id,),
            ).fetchall()
        return [Candidate.model_validate_json(r["data"]) for r in rows]

    # -- checkpoints (the crash-recovery log) ------------------------------
    def save_checkpoint(
        self,
        campaign_id: str,
        stage: str,
        context: dict[str, Any],
        candidate_id: str | None = None,
    ) -> Checkpoint:
        """Append a checkpoint capturing the full working context.

        **HM-Pro Lesson 1**: this is THE mechanism that prevents blind rework.
        VERIFY→DESIGN must checkpoint ``verify_findings`` so DESIGN receives
        them. Call after every state transition, inside the same transaction
        as the campaign update (see ``commit_transition``).
        """
        created_at = datetime.now().isoformat()
        context_json = json.dumps(context, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO checkpoints (campaign_id, stage, candidate_id, context, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (campaign_id, stage, candidate_id, context_json, created_at),
            )
            seq = cur.lastrowid
        return Checkpoint(
            seq=seq,
            campaign_id=campaign_id,
            stage=stage,
            candidate_id=candidate_id,
            context=context,
            created_at=datetime.fromisoformat(created_at),
        )

    def latest_checkpoint(self, campaign_id: str) -> Checkpoint | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT seq, campaign_id, stage, candidate_id, context, created_at
                FROM checkpoints WHERE campaign_id = ?
                ORDER BY seq DESC LIMIT 1
                """,
                (campaign_id,),
            ).fetchone()
        if not row:
            return None
        return Checkpoint(
            seq=row["seq"],
            campaign_id=row["campaign_id"],
            stage=row["stage"],
            candidate_id=row["candidate_id"],
            context=json.loads(row["context"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_checkpoints(self, campaign_id: str) -> list[Checkpoint]:
        """Full transition history for a campaign (newest last)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq, campaign_id, stage, candidate_id, context, created_at
                FROM checkpoints WHERE campaign_id = ?
                ORDER BY seq ASC
                """,
                (campaign_id,),
            ).fetchall()
        return [
            Checkpoint(
                seq=r["seq"],
                campaign_id=r["campaign_id"],
                stage=r["stage"],
                candidate_id=r["candidate_id"],
                context=json.loads(r["context"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # -- atomic transitions ------------------------------------------------
    def commit_transition(
        self,
        campaign: Campaign,
        stage: str,
        context: dict[str, Any],
        candidates: list[Candidate] | None = None,
    ) -> Checkpoint:
        """Atomically persist a state transition.

        Writes the campaign (and any dirty candidates) AND a checkpoint in a
        single transaction, so a crash mid-transition leaves either the old
        state or the new state + checkpoint — never a half-written one. This
        is the call the pipeline makes after every stage.
        """
        campaign.touch()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self.save_campaign(campaign)
                if candidates:
                    for cand in candidates:
                        self.save_candidate(cand)
                checkpoint = self.save_checkpoint(
                    campaign.id, stage, context, campaign.current_candidate_id
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return checkpoint

    # -- restore -----------------------------------------------------------
    def restore(self, campaign_id: str) -> ResumeSnapshot | None:
        """Rebuild the full resumable view of a campaign after a restart.

        Returns the campaign, its candidate queue, and the latest checkpoint
        (whose ``context`` carries the working memory needed to resume). None
        if the campaign doesn't exist.
        """
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return None
        candidates = self.list_candidates(campaign_id)
        checkpoint = self.latest_checkpoint(campaign_id)
        return ResumeSnapshot(
            campaign=campaign, candidates=candidates, checkpoint=checkpoint
        )

    # -- aggregate stats (for `haa report`) --------------------------------
    def stats(self) -> dict[str, Any]:
        """Cheap aggregate counts/sums for the dashboard and CLI report."""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM campaigns").fetchone()["n"]
            by_status = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM campaigns GROUP BY status"
            ).fetchall()
        # budget_used lives inside the JSON `data` column, so total it from the
        # deserialised rows (cheap for the dashboard's typical row count).
        campaigns = self.list_campaigns()
        total_used = sum(c.budget_used for c in campaigns)
        return {
            "total_campaigns": total,
            "by_status": {r["status"]: r["n"] for r in by_status},
            "total_budget_used": round(total_used, 4),
            "published": sum(
                1 for c in campaigns if c.status == CampaignStatus.PUBLISHED
            ),
        }

    # -- observability events (Phase 6a-fronted) --------------------------
    def save_event(
        self,
        *,
        event_type: str,
        campaign_id: str | None = None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
        duration_s: float = 0.0,
    ) -> Event:
        """Append one measurement event to the ``events`` log.

        Events are the per-call measurement stream (cost / tokens / duration /
        truncation) that ``analyze_campaign`` aggregates for Phase 5 tuning.
        Keep payloads small — large transcripts belong in the LLM log lines, not
        here.
        """
        created_at = datetime.now()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events
                    (campaign_id, stage, event_type, payload, cost_usd, tokens, duration_s, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id, stage, event_type, payload_json,
                    float(cost_usd), int(tokens), float(duration_s),
                    created_at.isoformat(),
                ),
            )
            seq = cur.lastrowid
        return Event(
            seq=seq, campaign_id=campaign_id, stage=stage, event_type=event_type,
            payload=payload or {}, cost_usd=cost_usd, tokens=tokens,
            duration_s=duration_s, created_at=created_at,
        )

    def sum_tokens(self, campaign_id: str | None = None) -> int:
        """events 表 llm_call 的 tokens 汇总（token 帽数据源，smoke10 检视项①）。"""
        if campaign_id is None:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM events WHERE event_type='llm_call'"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM events WHERE event_type='llm_call' AND campaign_id=?",
                (campaign_id,),
            ).fetchone()
        return int(row[0] or 0)

    def delete_campaign_events(self, campaign_id: str) -> int:
        """B4 轮转：删除指定 campaign 的事件（归档后调用）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE campaign_id = ?", (campaign_id,))
            self._conn.commit()
            return cur.rowcount

    def list_events(self, campaign_id: str | None = None) -> list[Event]:
        """All events (optionally for one campaign), oldest first."""
        query = (
            "SELECT seq, campaign_id, stage, event_type, payload, cost_usd, "
            "tokens, duration_s, created_at FROM events"
        )
        params: tuple = ()
        if campaign_id is not None:
            query += " WHERE campaign_id = ?"
            params = (campaign_id,)
        query += " ORDER BY seq ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            Event(
                seq=r["seq"], campaign_id=r["campaign_id"], stage=r["stage"],
                event_type=r["event_type"],
                payload=json.loads(r["payload"] or "{}"),
                cost_usd=r["cost_usd"] or 0.0, tokens=r["tokens"] or 0,
                duration_s=r["duration_s"] or 0.0,
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
