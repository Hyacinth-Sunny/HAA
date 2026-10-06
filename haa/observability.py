"""Observability layer (Phase 6a, fronted into Phase 5).

Two concerns live here:

1. **Logging setup** (:func:`setup_logging`) — wires real handlers on the root
   logger so the per-call ``logger.info`` lines emitted by the LLM client /
   agent loop are actually visible (without this, Python's lastResort handler
   drops everything below WARNING). Supports text/JSON formatting and
   stdout/file output.

2. **Structured event stream** (:class:`EventSink` / :class:`SQLiteEventSink`,
   added in B2) — an append-only per-call record (cost, tokens, duration,
   truncated, …) persisted to the ``events`` table, giving Phase 5 tuning and
   the Phase 6a mismatch detector the data they need. Today's
   ``budget_used`` is only a campaign-level accumulator; this is the
   per-stage granularity that ``analyze_campaign`` (B4) aggregates.

Importing this module is cheap (no heavy deps). ``setup_logging`` is the only
public call sites need at startup.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

from haa.config import Config, LoggingConfig

if TYPE_CHECKING:
    from haa.state import StateStore


# ---------------------------------------------------------------------------
# JSON formatting (no external dependency)
# ---------------------------------------------------------------------------


# Attributes logging sets on every LogRecord that we should NOT copy through as
# "extra" fields (they're already represented or are noise).
_RECORD_BUILTINS = frozenset(
    {
        "msg", "args", "levelname", "levelno", "name", "created", "msecs",
        "relativeCreated", "exc_info", "exc_text", "stack_info", "lineno",
        "funcName", "filename", "module", "pathname", "thread", "threadName",
        "process", "processName", "taskName", "getMessage",
    }
)


class JsonFormatter(logging.Formatter):
    """Minimal one-line JSON formatter (no python-json-logger dependency)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Carry through any caller-attached "extra" attributes.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RECORD_BUILTINS:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def setup_logging(config: Config | LoggingConfig | None = None) -> logging.Logger:
    """Wire real handlers on the root logger so INFO isn't silently dropped.

    Idempotent: clears existing root handlers first, so repeated calls (e.g.
    CLI then server) don't stack duplicates. Call once at process start
    (``haa run``, ``api.server.create_app``).

    Parameters
    ----------
    config:
        A full :class:`~haa.config.Config` (reads ``config.logging``), a
        :class:`~haa.config.LoggingConfig`, or ``None`` for built-in defaults.
    """
    if config is None:
        cfg = LoggingConfig()
    elif isinstance(config, Config):
        cfg = config.logging
    else:
        cfg = config

    level = getattr(logging, cfg.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers (idempotent re-setup).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if cfg.output == "file":
        Path(cfg.file_path).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(cfg.file_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    if cfg.format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(handler)
    return root


# ---------------------------------------------------------------------------
# Event sink — structured per-call measurement stream (B2)
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    """Where measurement events go.

    Decouples :class:`~haa.llm.client.LLMClient` / AgentLoop from
    :mod:`haa.state` — they depend on this Protocol, not on StateStore. The
    pipeline injects a :class:`SQLiteEventSink`; tests can swap in a no-op.
    """

    def emit(
        self,
        *,
        event_type: str,
        campaign_id: str | None = None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        ...


class NullEventSink:
    """Default no-op sink: emits nothing (the behaviour when no sink is wired)."""

    def emit(
        self,
        *,
        event_type: str,
        campaign_id: str | None = None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        return None


class SQLiteEventSink:
    """Persist events to ``StateStore.events`` via :meth:`save_event`.

    ``campaign_id`` is passed per-emit (an LLM client serves many campaigns),
    not fixed on the sink, so one sink serves a whole pipeline run. ``emit``
    never raises — the observability layer must not crash the pipeline.
    """

    def __init__(self, store: "StateStore") -> None:
        self.store = store

    def emit(
        self,
        *,
        event_type: str,
        campaign_id: str | None = None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        try:
            self.store.save_event(
                event_type=event_type,
                campaign_id=campaign_id,
                stage=stage,
                payload=payload,
                cost_usd=cost_usd,
                tokens=tokens,
                duration_s=duration_s,
            )
        except Exception:  # observability must never crash the pipeline
            pass


# ---------------------------------------------------------------------------
# Campaign analysis — aggregate the event stream (B4)
# ---------------------------------------------------------------------------


def analyze_campaign(store: "StateStore", campaign_id: str) -> dict[str, Any]:
    """Aggregate a campaign's event stream into tuning signals.

    Returns per-stage call counts / cost, the count of ``stage_truncated``
    events (a stage hitting its ``max_tool_calls`` cap), LLM failures, and a
    simple mean+3σ cost-outlier list. Read-only; never mutates the store. This
    is the data Phase 5 tuning and the Phase 6a mismatch detector build on.
    """
    events = store.list_events(campaign_id)
    if not events:
        return {
            "campaign_id": campaign_id,
            "event_count": 0,
            "by_stage": {},
            "by_tool": {},
            "truncated_stages": [],
            "llm_failures": [],
            "anomalies": [],
            "total_cost": 0.0,
        }

    stage_costs: dict[str | None, list[float]] = {}
    stage_calls: dict[str | None, int] = {}
    truncated: list[str] = []
    llm_failures: list[str] = []
    all_costs: list[tuple[float, str | None, int]] = []  # (cost, stage, seq)
    tool_events: list = []

    for e in events:
        stage = e.stage
        if e.event_type == "llm_call":
            stage_costs.setdefault(stage, []).append(e.cost_usd)
            stage_calls[stage] = stage_calls.get(stage, 0) + 1
            if e.cost_usd > 0:
                all_costs.append((e.cost_usd, stage, e.seq))
        elif e.event_type == "stage_truncated":
            if stage:
                truncated.append(stage)
        elif e.event_type == "llm_call_failed":
            if stage:
                llm_failures.append(stage)
        elif e.event_type == "tool_call":
            tool_events.append(e)

    by_stage: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    for stage, costs in stage_costs.items():
        s_total = sum(costs)
        total_cost += s_total
        by_stage[stage or "(no stage)"] = {
            "calls": stage_calls.get(stage, 0),
            "total_cost": round(s_total, 6),
            "mean_cost": round(s_total / len(costs), 6) if costs else 0.0,
        }

    # mean + 3σ outlier detection across all LLM calls (needs ≥3 samples)
    anomalies: list[dict[str, Any]] = []
    cost_stats = None
    if len(all_costs) >= 3:
        vals = [c for c, _, _ in all_costs]
        mean = sum(vals) / len(vals)
        var = sum((c - mean) ** 2 for c in vals) / len(vals)
        std = var ** 0.5
        threshold = mean + 3 * std
        cost_stats = {
            "mean": round(mean, 6),
            "std": round(std, 6),
            "threshold_3sigma": round(threshold, 6),
        }
        for cost, stage, seq in all_costs:
            if cost > threshold:
                anomalies.append(
                    {"seq": seq, "stage": stage, "cost": round(cost, 6),
                     "reason": "cost > mean+3σ"}
                )

    return {
        "campaign_id": campaign_id,
        "event_count": len(events),
        "by_stage": by_stage,
        "by_tool": _aggregate_tool_events(tool_events),
        "truncated_stages": truncated,
        "llm_failures": llm_failures,
        "cost_stats": cost_stats,
        "anomalies": anomalies,
        "total_cost": round(total_cost, 6),
    }


def _aggregate_tool_events(events: list) -> dict[str, dict[str, Any]]:
    """tool_call 事件 → per-tool / per-stage×tool 统计（调参数据源）。

    stage_tool_limits 的历次调参（v0.9.1、v1.0.2）都靠人工翻日志；有此表后
    撞帽/失败率/耗时可直接查询。"""
    by_tool: dict[str, dict[str, Any]] = {}
    for e in events:
        p = e.payload if isinstance(e.payload, dict) else {}
        tool = str(p.get("tool", "?"))
        ok = bool(p.get("ok", True))
        entry = by_tool.setdefault(
            tool, {"calls": 0, "failures": 0, "total_duration_s": 0.0, "by_stage": {}}
        )
        entry["calls"] += 1
        if not ok:
            entry["failures"] += 1
        entry["total_duration_s"] += e.duration_s or 0.0
        if e.stage:
            entry["by_stage"][e.stage] = entry["by_stage"].get(e.stage, 0) + 1
    for entry in by_tool.values():
        entry["total_duration_s"] = round(entry["total_duration_s"], 3)
        entry["failure_rate"] = (
            round(entry["failures"] / entry["calls"], 4) if entry["calls"] else 0.0
        )
    return by_tool


def tool_call_stats(store: "StateStore", campaign_id: str | None = None) -> dict[str, Any]:
    """跨项目/单项目的工具调用统计总览（/api/tool-stats 数据源）。"""
    events = [e for e in store.list_events(campaign_id) if e.event_type == "tool_call"]
    return {
        "campaign_id": campaign_id,
        "total_calls": len(events),
        "by_tool": _aggregate_tool_events(events),
    }
