"""Tests for the five-page HAA web frontend (Home/Projects/Logs/Settings/About)
plus the new read-only + write API endpoints added for the frontend.

Uses TestClient + a temp config (temp DB + campaigns dir) like the existing
server tests. No real LLM calls: lifecycle operations are only exercised for
their synchronous state transitions; async P1/P2/P3 ops are background tasks.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from haa.config import Config, StorageConfig
from haa.models import Brief, Phase, Precursor, Project, ProjectStatus


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


def _create_project(client, **over):
    data = {"title": "T", "problem_area": "P", "track": "theory"}
    data.update(over)
    r = client.post("/projects", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/projects/")[1]


def test_five_pages_reachable(client):
    for path in ("/", "/projects", "/projects/new", "/logs", "/settings", "/about"):
        r = client.get(path)
        assert r.status_code == 200, path
        # 版本无关断言：footer 版本号随发布递增，不应每次升版都改测试
        assert "HAA V1." in r.text
        assert "htmx.min.js" in r.text


def test_nav_has_five_buttons(client):
    r = client.get("/")
    for label in ("主页", "项目情况", "日志查看", "设置", "关于"):
        assert label in r.text
    assert r.text.count("<a href=") >= 5


def test_bilingual_switch(client):
    zh = client.get("/")
    en = client.get("/?lang=en")
    assert "主页" in zh.text and "Home" in en.text
    assert "项目情况" in zh.text and "Projects" in en.text
    assert "日志查看" in zh.text and "Logs" in en.text
    assert "设置" in zh.text and "Settings" in en.text
    assert "关于" in zh.text and "About" in en.text
    assert "自动科研与论文生产工具" in zh.text
    assert "Automated Research" in en.text


def test_projects_api_empty_and_after_create(client):
    assert client.get("/api/projects").json() == []
    pid = _create_project(client)
    body = client.get(f"/api/projects/{pid}").json()
    assert body["project"]["id"] == pid
    assert body["project"]["phase"] == "pr"
    assert body["project"]["status"] == "not_started"
    assert body["linked_campaigns"] == []
    listed = client.get("/api/projects").json()
    assert [p["id"] for p in listed] == [pid]


def test_project_api_404(client):
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/projects/nope/events").status_code == 404


def test_events_api(client):
    store = client.app.state.store
    e1 = store.save_event(event_type="llm_call", campaign_id="c1", stage="SEEK",
                          payload={"model": "m"}, cost_usd=0.01, tokens=10)
    e2 = store.save_event(event_type="stage_truncated", campaign_id="c1", stage="WRITE")
    body = client.get("/api/events").json()
    assert [e["seq"] for e in body] == [e1.seq, e2.seq]
    filtered = client.get("/api/events", params={"campaign_id": "c1", "limit": 1}).json()
    assert len(filtered) == 1 and filtered[0]["event_type"] == "stage_truncated"


def test_project_events_api(client):
    store = client.app.state.store
    pid = _create_project(client)
    cid = client.post("/api/campaigns", json={
        "title": "T", "problem_area": "P", "track": "theory",
    }).json()["id"]
    store.link_campaign(pid, cid, "scout")
    store.save_event(event_type="llm_call", campaign_id=cid, stage="SEEK",
                     cost_usd=0.01, tokens=5)
    body = client.get(f"/api/projects/{pid}/events").json()
    assert len(body) == 1 and body[0]["campaign_id"] == cid


def test_config_api_redacts_secrets(client):
    body = client.get("/api/config").json()
    assert body["llm"]["model"]
    assert body["llm"]["api_key_env"]
    dump = json.dumps(body)
    assert "sk-" not in dump and "OPENAI_API_KEY_VALUE" not in dump
    assert body["budget"]["per_campaign"] > 0
    assert body["pipeline"]["seek_candidate_count"] > 0
    assert body["tools"]["exec_bash"]["blocked_commands"]


def test_prompts_api(client):
    names = client.get("/api/prompts").json()
    assert "seek" in names and "p3_md_to_latex" in names and len(names) >= 15
    body = client.get("/api/prompts/seek").json()
    assert body["name"] == "seek" and body["content"]
    assert client.get("/api/prompts/does-not-exist").status_code == 404
    html = client.get("/partials/prompts/seek?lang=zh")
    assert html.status_code == 200 and "seek.md" in html.text


def test_create_project_api_json(client):
    r = client.post("/api/projects", json={
        "brief": {"title": "J", "problem_area": "Q", "track": "theory"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phase"] == "pr" and body["status"] == "not_started"
    assert body["hyperparams"]["seek_base_count"] == 5


def test_projects_list_renders_rows(client):
    pid = _create_project(client, title="边界上界", seek_base_count="7")
    r = client.get("/projects")
    assert r.status_code == 200
    assert "边界上界" in r.text
    assert pid[:12] in r.text
    assert "not_started" in r.text


def test_project_detail_pr_phase(client):
    pid = _create_project(client, title="PR项目", constraints="c1\nc2", exclusions="e1")
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "PR项目" in r.text
    assert "c1" in r.text and "e1" in r.text
    assert "启动 P1" in r.text
    assert f'action="/projects/{pid}/start"' in r.text


def test_project_detail_arv_precursors(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.status = ProjectStatus.IN_PROGRESS
    p.phase = Phase.ARV
    p.precursors = [Precursor(
        campaign_id="a" * 32, candidate_id="b" * 32, candidate_slug="fast-conv",
        candidate_title="快速卷积上界", grade="solid",
        paper={"abstract": "我们证明了一个新上界"},
        review={"correctness": 4, "quality": 5, "industry": 3},
        exp_spec={"objective": "验证", "metrics": "MAE"},
    )]
    store.save_project(p)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "快速卷积上界" in r.text
    assert "GRADE: solid" in r.text
    assert "我们证明了一个新上界" in r.text
    assert 'name="campaign_ids"' in r.text  # v1.0.2 多选 checkbox


def test_project_detail_ea_metrics(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.phase = Phase.EA
    p.status = ProjectStatus.IN_PROGRESS
    p.exp_results = {
        "metrics": {"acc": 0.92},
        "analysis": {"summary": "实验稳定收敛", "figures": ["loss curve"]},
        "rounds_a": 2, "rounds_b": 1,
    }
    store.save_project(p)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "acc" in r.text and "0.92" in r.text
    assert "实验稳定收敛" in r.text
    assert "进入 P3" in r.text


def test_project_detail_done_deliverable(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.phase = Phase.DONE
    p.p3_deliverable_path = "/tmp/deliverable.zip"
    p.p3_paper_dir = "/tmp/paper"
    store.save_project(p)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "deliverable.zip" in r.text
    assert "批准完成" in r.text


def test_project_detail_moribund_banner(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.status = ProjectStatus.MORIBUND
    p.phase = Phase.P2
    p.moribund_reason = "所有候选被否决"
    p.moribund_diagnostic = "简报问题域过宽"
    store.save_project(p)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "濒死" in r.text
    assert "简报问题域过宽" in r.text
    # The moribund banner now offers rework buttons (macro reverse arrows)
    # instead of the old plain recover form; moribund:p2 allows p1/p2/arv.
    assert "/rework" in r.text
    assert 'name="target" value="p2"' in r.text
    assert 'name="target" value="arv"' in r.text
    assert 'name="target" value="p1"' in r.text
    # The diagnostic renders through the markdown filter (md-body panel).
    assert "md-body" in r.text


def test_unknown_project_page_404(client):
    assert client.get("/projects/nope").status_code == 404


def test_select_recover_abort_complete(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.status = ProjectStatus.IN_PROGRESS
    p.phase = Phase.ARV
    p.precursors = [Precursor(
        campaign_id="c" * 32, candidate_id="d" * 32, candidate_slug="abc",
        candidate_title="A", grade="thin",
    )]
    store.save_project(p)

    r = client.post(f"/api/projects/{pid}/select", data={"campaign_id": "c" * 32})
    assert r.status_code == 200
    assert r.json()["selected_precursor_campaign_id"] == "c" * 32

    r = client.post(f"/api/projects/{pid}/select", data={"campaign_id": "zz" * 16})
    assert r.status_code == 409

    p = store.get_project(pid)
    p.status = ProjectStatus.MORIBUND
    store.save_project(p)
    r = client.post(f"/api/projects/{pid}/recover")
    assert r.status_code == 200 and r.json()["status"] == "in_progress"

    r = client.post(f"/api/projects/{pid}/abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"
    assert client.post(f"/api/projects/{pid}/abort").status_code == 409


def test_logs_page_renders_and_filters(client):
    store = client.app.state.store
    store.save_event(event_type="llm_call", campaign_id="c1", stage="SEEK",
                     payload={"model": "m"}, cost_usd=0.003, tokens=1234)
    store.save_event(event_type="stage_truncated", campaign_id="c1", stage="WRITE")
    r = client.get("/logs")
    assert r.status_code == 200
    assert "llm_call" in r.text and "stage_truncated" in r.text

    r = client.get("/logs", params={"event_type": "llm_call"})
    assert r.status_code == 200
    # the filter dropdown still lists all types; the table must not show it
    assert "stage_truncated" not in r.text.split('<div class="table-wrap">')[1]


def test_project_partials_include_polling_attributes(client):
    pid = _create_project(client)
    store = client.app.state.store
    p = store.get_project(pid)
    p.status = ProjectStatus.IN_PROGRESS
    p.phase = Phase.P1
    store.save_project(p)
    html = client.get(f"/partials/projects/{pid}/status").text
    assert 'hx-trigger="every 2s"' in html
    html2 = client.get(f"/partials/projects/{pid}/progress").text
    assert 'hx-trigger="every 3s"' in html2
