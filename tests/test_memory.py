"""Tests for the v1.0 cross-project memory store."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from haa.memory import MemoryIdea, MemoryStore, idea_pages_from_campaign


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path / "memory", max_inject=8)


def _failed(slug="gated-rl", **kw):
    base = dict(
        slug=slug, title="Gated RL for tuning", status="failed",
        domain_keywords=["rl", "tuning"],
        failure_reason="PPO on-policy cannot do counterfactual",
        kill_stage="EXP_FEASIBILITY",
    )
    base.update(kw)
    return MemoryIdea(**base)


class TestMemoryIdea:
    def test_failed_requires_reason(self):
        with pytest.raises(ValidationError):
            MemoryIdea(slug="x", title="X", status="failed")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            MemoryIdea(slug="x", title="X", status="bogus")

    def test_page_roundtrip(self, tmp_path):
        idea = _failed()
        page = idea.to_page()
        assert "failure_reason" in page
        # Write and reload through the store's parser path.
        d = tmp_path / "ideas"
        d.mkdir()
        (d / "gated-rl.md").write_text(page, encoding="utf-8")
        from haa.memory import _load_idea
        loaded = _load_idea(d / "gated-rl.md")
        assert loaded is not None
        assert loaded.slug == "gated-rl"
        assert loaded.status == "failed"
        assert loaded.kill_stage == "EXP_FEASIBILITY"
        assert loaded.domain_keywords == ["rl", "tuning"]


class TestMemoryStore:
    def test_record_and_list(self, mem):
        mem.record_idea(_failed())
        mem.record_idea(MemoryIdea(slug="other", title="Other", status="proposed"))
        ideas = mem.list_ideas()
        assert {i.slug for i in ideas} == {"gated-rl", "other"}

    def test_derivations_contain_tombstone(self, mem):
        mem.record_idea(_failed())
        mem.rebuild_derivations()
        brief = (mem.root / "context_brief.md").read_text(encoding="utf-8")
        assert "gated-rl" in brief
        assert "EXP_FEASIBILITY" in brief
        oq = (mem.root / "open_questions.md").read_text(encoding="utf-8")
        assert "无未决方向" in oq or "未死透" in oq

    def test_record_batch_rebuilds(self, mem):
        mem.record_batch([_failed(), MemoryIdea(slug="open", title="Open", status="tested")])
        assert (mem.root / "context_brief.md").exists()
        oq = (mem.root / "open_questions.md").read_text(encoding="utf-8")
        assert "Open" in oq

    def test_brief_for_hits_by_keyword(self, mem):
        mem.record_idea(_failed())
        from haa.models import Brief, Track

        class FakeBrief:
            title = "RL tuning with knowledge"
            problem_area = "database knob tuning via reinforcement learning"
            constraints = []

        text = mem.brief_for(FakeBrief())
        assert "gated-rl" in text
        assert "请勿重复" in text
        assert "历史记忆" in text

    def test_brief_for_empty_when_no_overlap(self, mem):
        mem.record_idea(_failed(domain_keywords=["biology"]))
        from haa.models import Brief

        class FakeBrief:
            title = "quantum error correction"
            problem_area = "topological codes"
            constraints = []

        assert mem.brief_for(FakeBrief()) == ""

    def test_injection_cap_respected(self, tmp_path):
        mem = MemoryStore(tmp_path / "m", max_inject=2)
        for i in range(5):
            mem.record_idea(_failed(slug=f"tuning-{i}", domain_keywords=["tuning", "rl"]))

        class FakeBrief:
            title = "tuning rl"
            problem_area = "tuning"
            constraints = []

        text = mem.brief_for(FakeBrief())
        assert text.count("死于") == 2  # capped at 2

    def test_never_injects_published(self, mem):
        mem.record_idea(MemoryIdea(
            slug="win", title="Winning tuning", status="published",
            failure_reason="", domain_keywords=["tuning"],
        ))
        mem.rebuild_derivations()

        class FakeBrief:
            title = "tuning"
            problem_area = "tuning"
            constraints = []

        text = mem.brief_for(FakeBrief())
        assert "win" not in text.lower() or "历史记忆" not in text


class TestCampaignTranscription:
    def test_killed_candidate_becomes_tombstone(self):
        candidates = [
            {"slug": "gated-rl", "title": "Gated RL", "status": "dead", "grade": "solid"},
            {"slug": "pub-idea", "title": "Published Idea", "status": "published"},
            {"slug": "capped", "title": "Capped", "status": "filtered"},
        ]
        kills = [{
            "stage": "EXP_FEASIBILITY", "slug": "gated-rl",
            "verdict": {"blockers": [{
                "severity": "fatal", "category": "protocol",
                "detail": "on-policy advantage cannot be counterfactual",
                "evidence": "PPO trains on-policy (Spinning Up)",
            }]},
        }]
        pages = idea_pages_from_campaign(
            "camp1", candidates, kills,
            origin_project="proj1", origin_brief_title="DB tuning",
        )
        by_slug = {p.slug: p for p in pages}
        assert by_slug["gated-rl"].status == "failed"
        assert by_slug["gated-rl"].kill_stage == "EXP_FEASIBILITY"
        assert "counterfactual" in by_slug["gated-rl"].failure_reason
        assert by_slug["pub-idea"].status == "published"
        assert by_slug["capped"].status == "proposed"

    def test_dead_without_kill_record(self):
        pages = idea_pages_from_campaign(
            "c", [{"slug": "x", "title": "X", "status": "dead"}], [],
            origin_project="p", origin_brief_title="t",
        )
        assert pages[0].status == "failed"
        assert "无 kill 记录" in pages[0].failure_reason


class TestControllerWiring:
    def test_p1_batch_records_to_memory(self, tmp_path):
        """After run_p1_batch, memory pages exist for both published and killed."""
        import yaml as _yaml
        from haa.config import load_config
        from haa.models import Brief, Track
        from haa.project_controller import ProjectController
        from haa.state import StateStore
        from tests.test_project_controller import FakePipeline, MockLLM

        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(_yaml.safe_dump({
            "storage": {
                "db_path": str(tmp_path / "t.db"),
                "campaigns_dir": str(tmp_path / "campaigns"),
            },
            "memory": {"memory_dir": str(tmp_path / "memory")},
        }))
        cfg = load_config(cfg_file)
        store = StateStore(str(tmp_path / "t.db"))

        brief = Brief(title="RL DB Tuning Memory Test", problem_area="knob tuning via RL",
                      track=Track.THEORY)
        pipe = FakePipeline(store, publish=True, scout_candidate_count=2)
        ctrl = ProjectController(cfg, store, budget=None, llm=MockLLM(), pipeline=pipe)
        project = ctrl.create_project(brief)
        ctrl.start_project(project.id)

        # Memory pages written for both candidates.
        mem = MemoryStore(tmp_path / "memory")
        ideas = mem.list_ideas()
        assert len(ideas) == 2
        statuses = {i.status for i in ideas}
        assert "published" in statuses
        assert (mem.root / "context_brief.md").exists()
        store.close()

    def test_seek_prompt_includes_tombstone(self, tmp_path):
        """_memory_brief_suffix injects tombstones into SEEK prompts."""
        import yaml as _yaml
        from haa.config import load_config
        from haa.models import Brief, Track
        from haa.stages.seek import SeekStage

        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(_yaml.safe_dump({
            "memory": {"memory_dir": str(tmp_path / "memory")},
        }))
        cfg = load_config(cfg_file)

        mem = MemoryStore(tmp_path / "memory")
        mem.record_idea(_failed())
        stage = SeekStage(None, cfg)
        suffix = stage._memory_brief_suffix(Brief(
            title="RL tuning", problem_area="knob tuning", track=Track.THEORY,
        ))
        assert "gated-rl" in suffix
        assert "历史记忆" in suffix

    def test_memory_disabled_no_suffix(self, tmp_path):
        """memory.enabled=False → empty suffix even with populated store."""
        import yaml as _yaml
        from haa.config import load_config
        from haa.models import Brief, Track
        from haa.stages.seek import SeekStage

        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(_yaml.safe_dump({
            "memory": {"enabled": False, "memory_dir": str(tmp_path / "memory")},
        }))
        cfg = load_config(cfg_file)
        MemoryStore(tmp_path / "memory").record_idea(_failed())
        stage = SeekStage(None, cfg)
        assert stage._memory_brief_suffix(Brief(
            title="RL tuning", problem_area="tuning", track=Track.THEORY,
        )) == ""
