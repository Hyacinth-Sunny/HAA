"""Phase 4b tests — the Jinja2+HTMX web surface (api/server.py HTML routes).

Uses TestClient + a temp config. The async ``run`` endpoint is driven with a
fake Pipeline so no real LLM runs. HTMX client behaviour isn't tested directly;
we assert the returned HTML fragments carry the right ``hx-*`` attributes and
real field names (guards against the CLI-style ``stage_name``/``grade_verdict``
field-name mistakes).
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


def _create(client, **over):
    data = {"title": "T", "problem_area": "P", "track": "theory"}
    data.update(over)
    r = client.post("/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/campaigns/")[1]


# --- route reachability ------------------------------------------------------

def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Hyacinth Automated Analyzer" in r.text
    assert "项目情况" in r.text and "日志查看" in r.text  # five-page nav
    assert "htmx.min.js" in r.text  # local static linked


def test_new_campaign_form_fields(client):
    r = client.get("/campaigns/new")
    assert r.status_code == 200
    assert 'name="title"' in r.text
    assert 'name="problem_area"' in r.text
    assert 'name="track"' in r.text


def test_unknown_campaign_404(client):
    for path in ("/campaigns/nope", "/campaigns/nope/paper",
                 "/partials/campaigns/nope/status", "/partials/campaigns/nope/progress"):
        assert client.get(path).status_code == 404


# --- form create -------------------------------------------------------------

def test_create_via_form_redirects_and_persists(client, tmp_path):
    r = client.post("/campaigns", data={
        "title": "UniqueT", "problem_area": "P", "track": "theory",
        "constraints": "c1\nc2", "exclusions": "e1",
    }, follow_redirects=False)
    assert r.status_code == 303
    cid = r.headers["location"].split("/campaigns/")[1]
    assert (tmp_path / "camps" / cid / "brief.json").exists()
    r2 = client.get(f"/campaigns/{cid}")
    assert "UniqueT" in r2.text
    assert "queued" in r2.text


def test_create_form_missing_required_field_422(client):
    # No title → FastAPI Form validation before the handler.
    r = client.post("/campaigns", data={"problem_area": "P"}, follow_redirects=False)
    assert r.status_code == 422


def test_create_form_brief_validation_renders_error(client):
    # Bad track → reaches handler, Brief() raises → rendered error page.
    r = client.post("/campaigns", data={"title": "T", "problem_area": "P", "track": "bogus"},
                    follow_redirects=False)
    assert r.status_code == 422
    assert "flash-error" in r.text


# --- status partial: polling self-stops on terminal -------------------------

def test_status_partial_polls_while_running(client):
    cid = _create(client)
    html = client.get(f"/partials/campaigns/{cid}/status").text
    assert 'hx-trigger="every 2s"' in html  # still polling
    assert "Run" in html  # not terminal → show Run button


def test_status_partial_stops_polling_when_terminal(client):
    cid = _create(client)
    store = client.app.state.store
    c = store.get_campaign(cid)
    c.status = CampaignStatus.PUBLISHED
    store.save_campaign(c)
    html = client.get(f"/partials/campaigns/{cid}/status").text
    assert 'hx-trigger="every 2s"' not in html  # polling stopped
    assert "PUBLISHED" in html


# --- real field names (guards against stage_name / grade_verdict mistakes) ---

def test_progress_partial_renders_real_stage(client):
    cid = _create(client)
    store = client.app.state.store
    c = store.get_campaign(cid)
    store.save_checkpoint(c.id, "SEEK:start", {"some": "ctx"})
    html = client.get(f"/partials/campaigns/{cid}/progress").text
    assert "SEEK:start" in html


def test_candidates_partial_empty_state(client):
    cid = _create(client)
    html = client.get(f"/partials/campaigns/{cid}/candidates").text
    assert "No candidates yet" in html


# --- paper viewer + version dropdown ----------------------------------------

def test_paper_empty_state(client):
    cid = _create(client)
    assert "not written yet" in client.get(f"/partials/campaigns/{cid}/paper").text


def test_paper_viewer_shows_latest_and_version_dropdown(client):
    cid = _create(client)
    store = client.app.state.store
    c = store.get_campaign(cid)
    store.save_checkpoint(c.id, "WRITE:done", {"paper": {"title": "P1", "intro": "v1"}}, None)
    store.save_checkpoint(c.id, "REFINE:done", {"paper": {"title": "P1", "intro": "v2"}}, None)
    html = client.get(f"/campaigns/{cid}/paper").text
    assert 'name="seq"' in html  # version dropdown
    assert "v2" in html  # latest shown by default
    # switch to historical version via partial
    seqs = [c2.seq for c2 in store.list_checkpoints(cid) if (c2.context or {}).get("paper")]
    html_old = client.get(f"/partials/campaigns/{cid}/paper?seq={seqs[0]}").text
    assert "v1" in html_old


# --- async run (BackgroundTasks + fake Pipeline) ----------------------------

def test_async_run_drives_pipeline(client, monkeypatch):
    calls = {}

    class FakePipeline:
        def __init__(self, config, store, budget):
            self.store = store

        def run_campaign(self, campaign_id, *, brief=None):
            calls["ran"] = True
            calls["brief"] = brief
            c = self.store.get_campaign(campaign_id)
            c.status = CampaignStatus.PUBLISHED
            self.store.save_campaign(c)
            return c

    monkeypatch.setattr(server_mod, "Pipeline", FakePipeline)
    cid = _create(client)
    r = client.post(f"/campaigns/{cid}/run")
    assert r.status_code == 200
    # TestClient runs background tasks to completion before returning.
    assert client.app.state.store.get_campaign(cid).is_terminal
    assert calls.get("ran") is True
    assert calls["brief"] is not None  # re-seeded from brief.json


def test_async_run_noop_when_terminal(client, monkeypatch):
    monkeypatch.setattr(server_mod, "Pipeline", lambda *a, **k: pytest.fail("should not run"))
    cid = _create(client)
    store = client.app.state.store
    c = store.get_campaign(cid)
    c.status = CampaignStatus.RETIRED
    store.save_campaign(c)
    r = client.post(f"/campaigns/{cid}/run")
    assert r.status_code == 200  # just returns the terminal partial
    assert "RETIRED" in r.text


# --- regression: the JSON /api run endpoint still works ---------------------

def test_api_sync_run_endpoint_unchanged(client, monkeypatch):
    class FakePipeline:
        def __init__(self, *a, **k):
            pass

        def run_campaign(self, campaign_id, *, brief=None):
            store = client.app.state.store
            c = store.get_campaign(campaign_id)
            c.status = CampaignStatus.PUBLISHED
            store.save_campaign(c)
            return c

    monkeypatch.setattr(server_mod, "Pipeline", FakePipeline)
    cid = _create(client)
    r = client.post(f"/api/campaigns/{cid}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "published"
