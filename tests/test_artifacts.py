"""Tests for the artifact store (v0.9.1): intermediate outputs on disk."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from haa.config import default_config
from haa.llm.tools import ToolError, ToolRegistry
from haa.models import Candidate
from haa.pipeline import Pipeline, StageName
from haa.state import StateStore
from haa.stages.base import BaseStage, StageResult, StageStatus

from tests.test_pipeline import MockStage, _make_pipeline, _result


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path, store):
    """Config whose campaigns_dir points at a temp dir (isolated artifacts)."""
    from haa.config import load_config
    import yaml as _yaml

    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(_yaml.safe_dump({
        "storage": {
            "db_path": str(tmp_path / "haa.db"),
            "campaigns_dir": str(tmp_path / "campaigns"),
        },
    }))
    return load_config(cfg_file)


@pytest.fixture
def campaign(store, brief):  # brief fixture comes from test_pipeline via conftest-style import
    return store.create_campaign(brief, budget_limit=20.0)


@pytest.fixture
def brief():
    from haa.models import Brief, Track
    return Brief(title="Artifacts", problem_area="AI", track=Track.THEORY)


@pytest.fixture
def budget(store):
    from haa.budget import BudgetManager
    return BudgetManager(store, global_limit=100.0)


class TestArtifactsOnDisk:
    def test_full_pipeline_writes_artifacts(self, cfg, store, budget, campaign, brief):
        """After a mock run: candidates.json, per-stage artifacts, MANIFEST."""
        candidates = [Candidate(
            campaign_id=campaign.id, slug="art-idea", title="Art Idea",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]
        stages = {
            StageName.SEEK: MockStage(_result(data={"candidates": candidates})),
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
        pipe = _make_pipeline(cfg, store, budget, stages)
        pipe.run_campaign(campaign.id, brief=brief)

        art = Path(cfg.storage.resolved_campaigns_dir()) / campaign.id / "artifacts"
        # candidates.json written from SEEK:done
        assert (art / "candidates.json").is_file()
        cands = json.loads((art / "candidates.json").read_text())
        assert cands[0]["slug"] == "art-idea"
        # MANIFEST exists, is JSON, and references real files
        manifest = json.loads((art / "MANIFEST.json").read_text())
        assert any(e["path"] == "candidates.json" for e in manifest)
        for entry in manifest:
            assert (art / entry["path"]).is_file(), f"manifest refs missing {entry['path']}"

    def test_stage_verdicts_persisted_per_candidate(self, cfg, store, budget, campaign, brief):
        """NOVELTY / GRADE verdicts land under artifacts/<slug>/."""
        candidates = [Candidate(
            campaign_id=campaign.id, slug="verdict-idea", title="V",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]
        stages = {
            StageName.SEEK: MockStage(_result(data={"candidates": candidates})),
            StageName.NOVELTY: MockStage(_result(data={
                "verdict": "NEW", "rationale": "no prior art", "closest_prior_work": "none",
            })),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"grade": "solid", "rationale": "concrete"})),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        pipe.run_campaign(campaign.id, brief=brief)

        art = Path(cfg.storage.resolved_campaigns_dir()) / campaign.id / "artifacts"
        nov = json.loads((art / "verdict-idea" / "novelty.json").read_text())
        assert nov["verdict"] == "NEW"
        g = json.loads((art / "verdict-idea" / "grade.json").read_text())
        assert g["grade"] == "solid"

    def test_rejected_candidate_archived(self, cfg, store, budget, campaign, brief):
        """A killed candidate's artifacts move to _rejected/<slug>/."""
        candidates = [Candidate(
            campaign_id=campaign.id, slug="doomed", title="Doomed",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]

        class NoveltyKiller(BaseStage):
            def run(self, camp, ctx):
                from haa.stages.base import StageStatus
                return StageResult(status=StageStatus.ABORT_CANDIDATE,
                                   data={"reason": "already solved"})

        stages = {
            StageName.SEEK: MockStage(_result(data={"candidates": candidates})),
            StageName.NOVELTY: NoveltyKiller(),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result()),
            StageName.WRITE: MockStage(_result()),
            StageName.REVIEW: MockStage(_result()),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        pipe.run_campaign(campaign.id, brief=brief)

        art = Path(cfg.storage.resolved_campaigns_dir()) / campaign.id / "artifacts"
        # The candidate dir is gone from active; _rejected holds the kill record
        # (kills.json stays top-level because extra survives resets).
        assert not (art / "doomed").exists()
        kills = json.loads((art / "kills.json").read_text())
        assert any(k.get("slug") == "doomed" for k in kills)


class TestReadFileGrounding:
    def test_not_found_error_lists_real_files(self, tmp_path):
        """read_file on a hallucinated path reports what actually exists."""
        from haa.config import ToolsConfig

        camp_dir = tmp_path / "c1"
        (camp_dir / "artifacts").mkdir(parents=True)
        (camp_dir / "brief.json").write_text("{}")
        (camp_dir / "artifacts" / "candidates.json").write_text("[]")

        reg = ToolRegistry(ToolsConfig(), campaigns_dir=str(tmp_path), allowed_roots=[])
        with pytest.raises(ToolError) as exc_info:
            reg.execute("read_file", {"path": "design.md"},
                        stage_name="DESIGN", campaign_id="c1")
        msg = str(exc_info.value)
        assert "brief.json" in msg
        assert "artifacts/candidates.json" in msg
        assert "conversation context" in msg


class TestPaperArtifact:
    def test_write_and_refine_persist_paper_json(self, cfg, store, budget, campaign, brief):
        """smoke4 复盘：旧 _stage_payload 取 context.write（恒 None）→ paper.json
        从未落盘。WRITE/REFINE 的产出在 context.paper，必须写 <slug>/paper.json。"""
        candidates = [Candidate(
            campaign_id=campaign.id, slug="paper-idea", title="Paper Idea",
            significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        )]
        stages = {
            StageName.SEEK: MockStage(_result(data={"candidates": candidates})),
            StageName.NOVELTY: MockStage(_result()),
            StageName.SCREEN: MockStage(_result()),
            StageName.DESIGN: MockStage(_result()),
            StageName.VERIFY: MockStage(_result()),
            StageName.GRADE: MockStage(_result(data={"verdict": "solid"})),
            StageName.WRITE: MockStage(_result(data={
                "title": "Paper Idea: Written", "abstract": "abs"})),
            StageName.REVIEW: MockStage(_result(data={"accept": True})),
            StageName.REFINE: MockStage(_result()),
            StageName.EXP_SPEC: MockStage(_result()),
            StageName.EXP_FEASIBILITY: MockStage(_result()),
            StageName.HUMAN_REVIEW: MockStage(_result()),
        }
        pipe = _make_pipeline(cfg, store, budget, stages)
        pipe.run_campaign(campaign.id, brief=brief)

        art = Path(cfg.storage.resolved_campaigns_dir()) / campaign.id / "artifacts"
        paper_path = art / "paper-idea" / "paper.json"
        assert paper_path.is_file(), "paper.json must land on disk after WRITE"
        paper = json.loads(paper_path.read_text())
        assert paper["title"] == "Paper Idea: Written"
        manifest = json.loads((art / "MANIFEST.json").read_text())
        assert any(e["path"] == "paper-idea/paper.json" for e in manifest)
