"""Tests for the observability layer (haa/observability.py + events table).

Covers: setup_logging (stdout/file/json/idempotent), SQLiteEventSink round-trip
+ Protocol conformance + never-raises, LLMClient emitting llm_call events,
analyze_campaign aggregation + 3σ outlier detection, and the truncated-signal
path end-to-end (stage._last_meta → Pipeline._observe → stage_truncated event).
"""

from __future__ import annotations

import logging

import pytest

from haa.budget import BudgetManager
from haa.config import BudgetConfig, Config, LoggingConfig, PipelineConfig, StorageConfig
from haa.llm.client import LLMClient
from haa.models import Brief, Campaign
from haa.observability import (
    EventSink,
    JsonFormatter,
    NullEventSink,
    SQLiteEventSink,
    analyze_campaign,
    setup_logging,
    tool_call_stats,
)
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "t.db")
    yield s
    s.close()


def _resp(content="hi", p=10, c=5):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
    }


# --- setup_logging -----------------------------------------------------------

def test_setup_logging_stdout():
    root = setup_logging(Config(logging=LoggingConfig()))
    assert root.level == logging.INFO
    assert len(root.handlers) == 1


def test_setup_logging_file(tmp_path):
    p = tmp_path / "haa.log"
    setup_logging(Config(logging=LoggingConfig(output="file", file_path=str(p))))
    logging.getLogger("haa.test").info("hello-file")
    assert p.exists()
    assert "hello-file" in p.read_text(encoding="utf-8")


def test_setup_logging_json_format():
    root = setup_logging(Config(logging=LoggingConfig(format="json")))
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_setup_logging_idempotent():
    setup_logging()
    setup_logging()
    assert len(logging.getLogger().handlers) == 1  # no duplicate handlers


# --- EventSink ---------------------------------------------------------------

def test_sqlite_sink_round_trip(store):
    sink = SQLiteEventSink(store)
    sink.emit(event_type="llm_call", campaign_id="c1", stage="SEEK",
              cost_usd=0.1, tokens=20, duration_s=1.5, payload={"model": "glm"})
    evs = store.list_events("c1")
    assert len(evs) == 1
    e = evs[0]
    assert e.event_type == "llm_call" and e.stage == "SEEK"
    assert e.cost_usd == 0.1 and e.tokens == 20 and e.duration_s == 1.5
    assert e.payload["model"] == "glm"


def test_null_sink_noop():
    NullEventSink().emit(event_type="x", cost_usd=1.0)  # must not raise


def test_sqlite_sink_is_event_sink(store):
    assert isinstance(SQLiteEventSink(store), EventSink)


def test_sqlite_sink_never_raises(store, monkeypatch):
    """A broken store must not crash the pipeline via the sink."""
    def boom(**kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "save_event", boom)
    SQLiteEventSink(store).emit(event_type="x")  # must not raise


# --- LLMClient emits events --------------------------------------------------

def test_llm_client_emits_llm_call_event(store):
    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    client = LLMClient(
        "glm-5.2",
        completion_fn=lambda **k: _resp(),
        cost_fn=lambda r: 0.05,
        event_sink=SQLiteEventSink(store),
    )
    client.call([{"role": "user", "content": "x"}], campaign_id=camp.id, stage="SEEK")
    evs = store.list_events(camp.id)
    assert any(e.event_type == "llm_call" and e.cost_usd == 0.05 and e.stage == "SEEK" for e in evs)


def test_llm_client_default_sink_emits_nothing(store):
    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    client = LLMClient("glm-5.2", completion_fn=lambda **k: _resp(), cost_fn=lambda r: 0.05)
    client.call([{"role": "user", "content": "x"}], campaign_id=camp.id, stage="SEEK")
    assert store.list_events(camp.id) == []


# --- analyze_campaign --------------------------------------------------------

def test_analyze_campaign_per_stage(store):
    cid = "camp1"
    for _ in range(3):
        store.save_event(event_type="llm_call", campaign_id=cid, stage="SEEK", cost_usd=0.1, tokens=10)
    for _ in range(2):
        store.save_event(event_type="llm_call", campaign_id=cid, stage="DESIGN", cost_usd=0.2, tokens=20)
    store.save_event(event_type="stage_truncated", campaign_id=cid, stage="VERIFY")
    r = analyze_campaign(store, cid)
    assert r["by_stage"]["SEEK"]["calls"] == 3
    assert r["by_stage"]["DESIGN"]["calls"] == 2
    assert r["by_stage"]["DESIGN"]["total_cost"] == pytest.approx(0.4)
    assert "VERIFY" in r["truncated_stages"]
    assert r["total_cost"] == pytest.approx(0.7)


def test_analyze_campaign_outlier(store):
    # 3σ needs enough normal samples that one large value is a real outlier
    # (with few samples the outlier inflates σ and hides itself).
    cid = "c"
    for _ in range(10):
        store.save_event(event_type="llm_call", campaign_id=cid, stage="SEEK", cost_usd=0.1, tokens=10)
    store.save_event(event_type="llm_call", campaign_id=cid, stage="SEEK", cost_usd=5.0, tokens=10)
    r = analyze_campaign(store, cid)
    assert r["cost_stats"] is not None
    assert len(r["anomalies"]) == 1
    assert r["anomalies"][0]["cost"] == pytest.approx(5.0)


def test_analyze_campaign_empty(store):
    r = analyze_campaign(store, "nonexistent")
    assert r["event_count"] == 0
    assert r["by_stage"] == {}


def test_analyze_campaign_no_outliers_with_few_samples(store):
    """3 calls is the floor; fewer → no stats, no anomalies."""
    cid = "c"
    store.save_event(event_type="llm_call", campaign_id=cid, stage="S", cost_usd=0.1, tokens=1)
    store.save_event(event_type="llm_call", campaign_id=cid, stage="S", cost_usd=9.0, tokens=1)
    r = analyze_campaign(store, cid)
    assert r["cost_stats"] is None
    assert r["anomalies"] == []


# --- truncated signal end-to-end --------------------------------------------

def test_stage_last_meta_records_truncated(monkeypatch):
    from haa.config import load_config
    from haa.llm.agent_loop import AgentLoopResult
    from haa.models import Brief
    from haa.stages import SeekStage
    from haa.stages.base import StageContext

    stage = SeekStage(llm=None, config=load_config())

    class FakeAgent:
        def run(self, prompt, **kw):
            return AgentLoopResult(content='{"ideas":[]}', truncated=True, iterations=10, messages=[])

    monkeypatch.setattr(stage, "_make_agent_loop", lambda: FakeAgent())
    camp = Campaign(brief_hash="x")
    stage.run(camp, StageContext(brief=Brief(title="T", problem_area="P")))
    assert stage._last_meta["truncated"] is True
    assert stage._last_meta["iterations"] == 10


def test_pipeline_observe_emits_truncated_event(store):
    from haa.pipeline import Pipeline

    cfg = Config(
        pipeline=PipelineConfig(),
        storage=StorageConfig(db_path=str(store.db_path)),
        budget=BudgetConfig(),
    )
    budget = BudgetManager(store, global_limit=cfg.budget.global_limit)
    pipe = Pipeline(cfg, store, budget, llm=object(), stages={})

    class FakeStage:
        _last_meta = {
            "truncated": True, "iterations": 5,
            "tool_calls_count": 10, "total_cost_usd": 0.3,
        }

    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    pipe._observe(FakeStage(), "SEEK", camp)
    evs = store.list_events(camp.id)
    assert any(e.event_type == "stage_truncated" and e.stage == "SEEK" for e in evs)
    assert any(e.event_type == "stage_measure" for e in evs)


def test_pipeline_obsume_no_meta_is_noop(store):
    from haa.pipeline import Pipeline

    cfg = Config(storage=StorageConfig(db_path=str(store.db_path)), budget=BudgetConfig())
    budget = BudgetManager(store, global_limit=cfg.budget.global_limit)
    pipe = Pipeline(cfg, store, budget, llm=object(), stages={})

    class FakeStage:
        _last_meta = None

    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    pipe._observe(FakeStage(), "SEEK", camp)
    assert store.list_events(camp.id) == []


# --- v1.0.6-rev2: tool_call 统计 ------------------------------------------------


def test_tool_call_stats_aggregates(store):
    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    sink = SQLiteEventSink(store)
    sink.emit(event_type="tool_call", campaign_id=camp.id, stage="SEEK",
              duration_s=1.0, payload={"tool": "grep", "ok": True, "error": None})
    sink.emit(event_type="tool_call", campaign_id=camp.id, stage="SEEK",
              duration_s=2.0, payload={"tool": "grep", "ok": False, "error": "x"})
    sink.emit(event_type="tool_call", campaign_id=camp.id, stage="VERIFY",
              duration_s=0.5, payload={"tool": "calculator", "ok": True, "error": None})
    sink.emit(event_type="llm_call", campaign_id=camp.id, stage="SEEK",
              cost_usd=0.01, tokens=10)

    stats = tool_call_stats(store, camp.id)
    assert stats["total_calls"] == 3
    g = stats["by_tool"]["grep"]
    assert g["calls"] == 2 and g["failures"] == 1
    assert g["failure_rate"] == 0.5
    assert g["total_duration_s"] == 3.0
    assert g["by_stage"] == {"SEEK": 2}
    assert stats["by_tool"]["calculator"]["by_stage"] == {"VERIFY": 1}
    # llm_call 事件不混入工具统计
    assert "web_search" not in stats["by_tool"]


def test_analyze_campaign_includes_by_tool(store):
    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    sink = SQLiteEventSink(store)
    sink.emit(event_type="tool_call", campaign_id=camp.id, stage="DESIGN",
              duration_s=1.0, payload={"tool": "calculator", "ok": True})
    out = analyze_campaign(store, camp.id)
    assert out["by_tool"]["calculator"]["calls"] == 1
