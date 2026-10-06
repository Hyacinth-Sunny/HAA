"""Tests for the FastAPI server (api/server.py).

Uses TestClient with a temp config (temp DB + campaigns dir). The ``run``
endpoint is tested with a fake Pipeline so no real LLM is called.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import server as server_mod
from api.server import create_app
from haa.config import Config, StorageConfig
from haa.models import CampaignStatus


def _config(tmp_path):
    return Config(
        storage=StorageConfig(
            db_path=str(tmp_path / "t.db"),
            campaigns_dir=str(tmp_path / "camps"),
        )
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(_config(tmp_path))) as c:
        yield c


def _brief_body():
    return {"title": "T", "problem_area": "P", "constraints": ["c1"], "track": "theory"}


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_tool_stats_endpoint(client):
    """GET /api/tool-stats：空库返回零调用，形状正确（v1.0.6-rev2）。"""
    r = client.get("/api/tool-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["by_tool"] == {}
    # 带 campaign_id 过滤不炸
    r2 = client.get("/api/tool-stats", params={"campaign_id": "nope"})
    assert r2.status_code == 200
    assert r2.json()["total_calls"] == 0


def test_create_campaign_persists_brief_and_returns_queued(client, tmp_path):
    r = client.post("/api/campaigns", json=_brief_body())
    assert r.status_code == 200, r.text
    camp = r.json()
    assert camp["status"] == "queued"
    cid = camp["id"]
    # brief.json saved next to the campaign
    assert (tmp_path / "camps" / cid / "brief.json").exists()


def test_list_and_get_campaign(client):
    created = client.post("/api/campaigns", json=_brief_body()).json()
    listed = client.get("/api/campaigns").json()
    assert [c["id"] for c in listed] == [created["id"]]

    got = client.get(f"/api/campaigns/{created['id']}").json()
    assert got["id"] == created["id"]


def test_get_unknown_campaign_404(client):
    assert client.get("/api/campaigns/nope").status_code == 404


def test_report_unknown_404(client):
    assert client.get("/api/campaigns/nope/report").status_code == 404


def test_report_structure(client):
    cid = client.post("/api/campaigns", json=_brief_body()).json()["id"]
    r = client.get(f"/api/campaigns/{cid}/report")
    assert r.status_code == 200
    body = r.json()
    assert body["campaign"]["id"] == cid
    assert body["candidates"] == []
    assert body["checkpoint_count"] == 0
    assert "budget" in body


def test_run_endpoint_drives_pipeline_and_returns_terminal(client, monkeypatch):
    """The run endpoint builds a Pipeline and calls run_campaign; we fake both."""
    calls = {}

    class FakePipeline:
        def __init__(self, config, store, budget):
            self.config, self.store, self.budget = config, store, budget

        def run_campaign(self, campaign_id, *, brief=None):
            calls["campaign_id"] = campaign_id
            calls["brief"] = brief
            c = self.store.get_campaign(campaign_id)
            c.status = CampaignStatus.PUBLISHED
            self.store.save_campaign(c)
            return c

    monkeypatch.setattr(server_mod, "Pipeline", FakePipeline)

    cid = client.post("/api/campaigns", json=_brief_body()).json()["id"]
    r = client.post(f"/api/campaigns/{cid}/run")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert calls["campaign_id"] == cid
    assert calls["brief"] is not None  # re-seeded from brief.json
    assert calls["brief"].title == "T"


def test_run_already_terminal_returns_409(client, monkeypatch):
    monkeypatch.setattr(server_mod, "Pipeline", lambda *a, **k: pytest.fail("should not run"))
    cid = client.post("/api/campaigns", json=_brief_body()).json()["id"]
    # Force terminal status directly via the store.
    store = client.app.state.store
    c = store.get_campaign(cid)
    c.status = CampaignStatus.RETIRED
    store.save_campaign(c)
    assert client.post(f"/api/campaigns/{cid}/run").status_code == 409


def test_stats(client):
    client.post("/api/campaigns", json=_brief_body())
    s = client.get("/api/stats").json()
    assert s["total_campaigns"] == 1
