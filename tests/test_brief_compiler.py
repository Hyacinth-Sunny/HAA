"""Tests for the brief compiler + knowledge-file module (v1.0.2).

Covers: free-form Markdown → Brief compilation (verbatim constraints,
deterministic path union), the unified loader's .md route, knowledge staging
into the campaign sandbox, worker-brief inheritance, and hash sensitivity.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from haa.brief_compiler import compile_brief, scan_knowledge_paths
from haa.config import default_config
from haa.models import Brief
from haa.project_controller import ProjectController


class FakeCompileLLM:
    """Duck-typed LLMClient: one JSON-mode call."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def call_json(self, messages, stage=None, **kw):
        self.calls.append({"messages": messages, "stage": stage})
        return self.payload, None


_MD = """### 研究简报：测试用例
**报告日期：** 2026-08-25

#### 一、问题阐述
要解决 X 问题，约束：ML 只能进优化层，绝不进正确性层。

**材料索引：**
- 研究包A：`/tmp/notes/01-研究包A.md`
- 研究包B：`/tmp/notes/02-研究包B.md`
"""

_PAYLOAD = {
    "title": "测试用例（Test Case）",
    "problem_area": "We study problem X with RTO 0.6s and throughput loss <=40%.",
    "constraints": ["ML 只能进优化层，绝不进正确性层"],
    "exclusions": ["不做更快的日志回放"],
    "track": "systems",
    "knowledge_files": ["/tmp/notes/01-研究包A.md"],
}


# --- compiler ----------------------------------------------------------------

def test_compile_brief_fields_and_path_union():
    client = FakeCompileLLM(dict(_PAYLOAD))
    brief = compile_brief(_MD, client)
    assert brief.title.startswith("测试用例")
    assert "RTO 0.6s" in brief.problem_area
    assert brief.constraints == ["ML 只能进优化层，绝不进正确性层"]  # 逐字
    # LLM 漏了 02-研究包B.md → regex 扫描兜底补上（确定性并集）
    assert brief.knowledge_files == [
        "/tmp/notes/01-研究包A.md", "/tmp/notes/02-研究包B.md",
    ]
    # 编译调用携带完整原文 + PR 阶段标记
    assert "研究简报" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["stage"] == "PR"


def test_scan_knowledge_paths_chinese_filenames():
    found = scan_knowledge_paths(
        "见 `/a/b/03-ExactCompare-双算精确比较.md` 与 dir `/a/b/`（目录不收）"
        "以及 `./rel/note.md`；裸词 report.pdf 不收（无歧义前缀）"
    )
    assert "/a/b/03-ExactCompare-双算精确比较.md" in found
    assert "./rel/note.md" in found
    assert all(not p.endswith("/") for p in found)
    assert "report.pdf" not in found


def test_compile_brief_rejects_empty_problem_area():
    payload = dict(_PAYLOAD, problem_area="   ")
    with pytest.raises(ValueError):
        compile_brief(_MD, FakeCompileLLM(payload))


# --- unified loader (.md route) ----------------------------------------------

def test_load_brief_md_routes_to_compiler(tmp_path, monkeypatch):
    import haa.brief_compiler as bc
    import haa.brief_io as bio

    seen: dict = {}

    def fake_compile(text, client):
        seen["text"] = text
        return Brief(title="T", problem_area="P", track="systems")

    monkeypatch.setattr(bc, "compile_brief", fake_compile)
    monkeypatch.setattr(bc, "build_compiler_client", lambda: object())
    p = tmp_path / "brief.md"
    p.write_text(_MD, encoding="utf-8")
    brief = bio.load_brief(p)
    assert brief.title == "T"
    assert "研究简报" in seen["text"]


def test_load_brief_yaml_still_works(tmp_path):
    import haa.brief_io as bio

    p = tmp_path / "brief.yaml"
    p.write_text("title: T\nproblem_area: P\n", encoding="utf-8")
    assert bio.load_brief(p).title == "T"


# --- knowledge staging + inheritance ------------------------------------------

def _mock_stages():
    """Minimal always-pass stage set (reuses test_pipeline doubles)."""
    from tests.test_pipeline import MockStage, _result
    from haa.pipeline import StageName

    return {
        StageName.SEEK: MockStage(_result(data={"candidates": []})),
        StageName.NOVELTY: MockStage(_result()),
        StageName.SCREEN: MockStage(_result()),
        StageName.DESIGN: MockStage(_result()),
        StageName.VERIFY: MockStage(_result()),
        StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
        StageName.WRITE: MockStage(_result()),
        StageName.REVIEW: MockStage(_result(data={"accept": True})),
        StageName.REFINE: MockStage(_result()),
        StageName.EXP_SPEC: MockStage(_result()),
        StageName.EXP_FEASIBILITY: MockStage(_result()),
        StageName.HUMAN_REVIEW: MockStage(_result()),
    }


def test_run_campaign_stages_knowledge_files(tmp_path, monkeypatch):
    """brief.knowledge_files → campaigns/<cid>/knowledge/<basename>."""
    from haa.budget import BudgetManager
    from haa.pipeline import Pipeline
    from haa.state import StateStore
    from tests.test_pipeline import _make_pipeline

    src1 = tmp_path / "note-a.md"
    src1.write_text("# A", encoding="utf-8")
    src2 = tmp_path / "note-b.md"
    src2.write_text("# B", encoding="utf-8")

    cfg = default_config()
    cfg = replace(
        cfg,
        storage=replace(cfg.storage, campaigns_dir=str(tmp_path / "camps")),
    )
    store = StateStore(str(tmp_path / "t.db"))
    budget = BudgetManager(store, global_limit=100.0)
    brief = Brief(
        title="T", problem_area="P", track="systems",
        knowledge_files=[str(src1), str(src2), "/nonexistent/x.md"],
    )
    campaign = store.create_campaign(brief, budget_limit=10.0)
    pipe = _make_pipeline(cfg, store, budget, _mock_stages())
    pipe.run_campaign(campaign.id, brief=brief)

    kdir = tmp_path / "camps" / campaign.id / "knowledge"
    assert (kdir / "note-a.md").read_text(encoding="utf-8") == "# A"
    assert (kdir / "note-b.md").is_file()
    # 幂等：重跑不炸、不重复复制
    pipe.run_campaign(campaign.id, brief=brief)
    assert (kdir / "note-a.md").is_file()


def test_worker_brief_inherits_knowledge_files():
    controller = ProjectController.__new__(ProjectController)  # 只测静态方法
    scout = Brief(
        title="T", problem_area="P", track="systems",
        constraints=["c1"], exclusions=["e1"],
        knowledge_files=["/a/b.md"],
    )
    worker = controller._make_worker_brief(scout)
    assert worker.skip_to == "novelty"
    assert worker.knowledge_files == ["/a/b.md"]
    assert worker.constraints == ["c1"]


def test_brief_hash_sensitive_to_knowledge():
    a = Brief(title="T", problem_area="P")
    b = Brief(title="T", problem_area="P", knowledge_files=["/x.md"])
    assert a.brief_hash() != b.brief_hash()


# --- web upload endpoint (compile → prefilled form → user confirms) -----------

def _web_config(tmp_path):
    from haa.config import Config, StorageConfig

    return Config(storage=StorageConfig(
        db_path=str(tmp_path / "t.db"),
        campaigns_dir=str(tmp_path / "camps"),
    ))


def test_upload_brief_compiles_and_prefills(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api.server import create_app
    import haa.brief_compiler as bc

    monkeypatch.setattr(
        bc, "compile_brief",
        lambda text, client: compile_brief(text, FakeCompileLLM(dict(_PAYLOAD))),
    )
    with TestClient(create_app(_web_config(tmp_path))) as c:
        r = c.post(
            "/projects/upload-brief",
            files={"file": ("brief.md", _MD.encode("utf-8"), "text/markdown")},
        )
        assert r.status_code == 200, r.text
        # 预填：标题/约束逐字出现，知识文件两条都在，含审阅提示
        assert "测试用例" in r.text
        assert "ML 只能进优化层，绝不进正确性层" in r.text
        assert "01-研究包A.md" in r.text and "02-研究包B.md" in r.text
        assert "请核对" in r.text


def test_create_project_form_accepts_knowledge_files(tmp_path):
    from fastapi.testclient import TestClient
    from api.server import create_app

    with TestClient(create_app(_web_config(tmp_path))) as c:
        r = c.post("/projects", data={
            "title": "T", "problem_area": "P", "track": "systems",
            "constraints": "c1", "exclusions": "",
            "knowledge_files": "/a/b.md\n/c/d.md",
            "seek_base_count": "3", "output_candidate_count": "2",
        }, follow_redirects=False)
        assert r.status_code == 303, r.text
        pid = r.headers["location"].split("/")[-1]
        r2 = c.get(f"/api/projects/{pid}")
        brief = r2.json()["project"]["brief"]
        assert brief["knowledge_files"] == ["/a/b.md", "/c/d.md"]


# --- v1.0.2 前端修复：ARV 多选 + 前体下载 + md 渲染 ---------------------------

def _arv_project(tmp_path):
    """建一个带 2 个前体、处于 ARV 的项目（直接操作 store，零 LLM）。"""
    from haa.state import StateStore
    from haa.models import Brief, Phase, Precursor, Project, ProjectStatus

    store = StateStore(str(tmp_path / "t.db"))
    project = Project(
        brief=Brief(title="T", problem_area="P", track="systems"),
        status=ProjectStatus.IN_PROGRESS,
        phase=Phase.ARV,
        precursors=[
            Precursor(
                campaign_id="camp-a", candidate_id="cand-a", candidate_slug="slug-a", candidate_title="Precursor A",
                grade="solid",
                paper={"title": "Paper A", "abstract": "**bold** abstract", "method": "## Method\nbody"},
                review={"overall": 0.7, "reports": {"quality": {"score": 4, "verdict": "fine"}}},
                exp_spec={"objective": "validate"},
            ),
            Precursor(
                campaign_id="camp-b", candidate_id="cand-b", candidate_slug="slug-b", candidate_title="Precursor B",
                grade="thin",
                paper={"title": "Paper B", "abstract": "plain"},
            ),
        ],
    )
    store.save_project(project)
    return store, project


def test_select_precursors_multi_and_lead(tmp_path):
    from haa.config import Config, StorageConfig
    from haa.project_controller import ProjectController
    from haa.budget import BudgetManager

    store, project = _arv_project(tmp_path)
    ctrl = ProjectController(
        Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "c"))),
        store, BudgetManager(store, global_limit=100.0),
    )
    result = ctrl.select_precursors(project.id, ["camp-b", "camp-a", "camp-b"])
    assert result.selected_precursor_campaign_ids == ["camp-b", "camp-a"]
    assert result.selected_precursor_campaign_id == "camp-b"  # 首个为主前体
    import pytest
    with pytest.raises(ValueError):
        ctrl.select_precursors(project.id, ["camp-a", "ghost"])


def test_arv_form_multi_select_and_download(tmp_path):
    from fastapi.testclient import TestClient
    from api.server import create_app
    from haa.config import Config, StorageConfig

    store, project = _arv_project(tmp_path)
    cfg = Config(storage=StorageConfig(
        db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "camps")))
    with TestClient(create_app(cfg)) as c:
        # 多选表单（此前该路由不存在 → 404）
        # httpx 的 data=[(k,v),…] 元组编码在本地有怪癖；用标准 urlencoded 体
        r = c.post(
            f"/projects/{project.id}/select",
            content="campaign_ids=camp-a&campaign_ids=camp-b",
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        p = store.get_project(project.id)
        assert p.selected_precursor_campaign_ids == ["camp-a", "camp-b"]
        assert p.selected_precursor_campaign_id == "camp-a"
        # 下载前体 .md
        r = c.get(f"/projects/{project.id}/precursors/camp-a/download")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        body = r.text
        assert "# Paper A" in body and "## METHOD" in body and "## REVIEW" in body
        # 不存在的前体 → 404
        assert c.get(f"/projects/{project.id}/precursors/ghost/download").status_code == 404


def test_md_filter_renders_and_sanitizes(tmp_path):
    from fastapi.testclient import TestClient
    from api.server import create_app
    from haa.config import Config, StorageConfig

    store, project = _arv_project(tmp_path)
    # 把简报问题域换成带 md + script 的内容，验证渲染与消毒
    p = store.get_project(project.id)
    p.brief.problem_area = "# Heading\n\n**bold** text\n\n<script>alert(1)</script>"
    store.save_project(p)
    cfg = Config(storage=StorageConfig(
        db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "camps")))
    with TestClient(create_app(cfg)) as c:
        r = c.get(f"/projects/{project.id}")
        assert r.status_code == 200
        assert "<h1>Heading</h1>" in r.text          # md 渲染生效
        assert "<strong>bold</strong>" in r.text       # 不被二次转义
        assert "<script>alert(1)</script>" not in r.text  # 消毒生效
