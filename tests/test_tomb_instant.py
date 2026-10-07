"""大修批次2（第二章 §8 墓穴即时版）锚定测试。

覆盖：_record_kill 扩展字段（core_claim/kill_evidence/structure_fingerprint）、
campaign_tomb_block 渲染与 1K-token 硬顶、load_campaign_kills 文件回退、
SEEK prompt 注入（第二轮起语义=有 kills 才注入）、记忆页携带 core_claim。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from haa.config import Config, StorageConfig, load_config
from haa.memory import (
    MemoryStore,
    campaign_tomb_block,
    idea_pages_from_campaign,
    load_campaign_kills,
)
from haa.models import Brief, Campaign, Candidate
from haa.pipeline import Pipeline
from haa.stages.base import StageContext, StageResult, StageStatus
from haa.stages import SeekStage
from tests.test_stages import FakeAgent, _brief, _campaign, _candidate


def _result(data=None):
    return StageResult(status=StageStatus.ABORT_CANDIDATE, data=data or {})


def _pipeline(tmp_path):
    cfg = Config(storage=StorageConfig(
        db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "camps")))
    return Pipeline(cfg, MagicMock(), MagicMock(), llm=MagicMock(), stages={})


# --------------------------------------------------------------------------- #
#  _record_kill 扩展字段
# --------------------------------------------------------------------------- #

class TestRecordKillEnrichment:

    def test_core_claim_from_positive_claim(self, tmp_path):
        pipe = _pipeline(tmp_path)
        ctx = StageContext(brief=_brief(), candidate=_candidate(_campaign()))
        pipe._record_kill("NOVELTY", ctx, _result({
            "reason": "duplicate", "evidence": "prior work X matches",
        }))
        k = ctx.extra["kills"][0]
        assert k["core_claim"] == "we achieve X"
        assert k["kill_evidence"] == "prior work X matches"
        assert k["slug"] == "cand" and k["stage"] == "NOVELTY"

    def test_kill_evidence_from_blockers(self, tmp_path):
        pipe = _pipeline(tmp_path)
        ctx = StageContext(brief=_brief(), candidate=_candidate(_campaign()))
        pipe._record_kill("EXP_FEASIBILITY", ctx, _result({
            "blockers": [
                {"severity": "fatal", "detail": "no dataset", "evidence": "E1"},
                {"severity": "major", "detail": "slow", "evidence": "E2"},
            ],
        }))
        k = ctx.extra["kills"][0]
        assert k["kill_evidence"] == "E1; E2"

    def test_structure_fingerprint_passthrough_and_absent_keys(self, tmp_path):
        pipe = _pipeline(tmp_path)
        ctx = StageContext(brief=_brief(), candidate=_candidate(_campaign()))
        fp = {"conflict_type": "WAW", "problem_class": ["冲突消解类"]}
        pipe._record_kill("SCREEN", ctx, _result({
            "reason": "counterexample", "structure_fingerprint": fp,
        }))
        k = ctx.extra["kills"][0]
        assert k["structure_fingerprint"] == fp
        # 无可抽证据/指纹时字段不出现（旧读方零感知）
        camp2 = _campaign()
        bare = _candidate(camp2)
        bare.positive_claim = ""
        bare.rationale = ""
        ctx2 = StageContext(brief=_brief(), candidate=bare)
        pipe._record_kill("GRADE", ctx2, _result({"reason": "trivial"}))
        k2 = ctx2.extra["kills"][0]
        assert "kill_evidence" not in k2 and "core_claim" not in k2
        assert "structure_fingerprint" not in k2

    def test_core_claim_falls_back_to_rationale(self, tmp_path):
        pipe = _pipeline(tmp_path)
        camp = _campaign()
        cand = _candidate(camp)
        cand.positive_claim = ""
        cand.rationale = "why worth doing"
        ctx = StageContext(brief=_brief(), candidate=cand)
        pipe._record_kill("NOVELTY", ctx, _result({"reason": "dup"}))
        assert ctx.extra["kills"][0]["core_claim"] == "why worth doing"


# --------------------------------------------------------------------------- #
#  campaign_tomb_block 渲染
# --------------------------------------------------------------------------- #

class TestCampaignTombBlock:

    def _kills(self):
        return [{
            "stage": "SCREEN", "slug": "cand", "title": "Fast Conv Bound",
            "core_claim": "we achieve X", "kill_evidence": "x<0 counterexample",
            "reason": "counterexample",
        }]

    def test_renders_stage_claim_reason(self):
        block = campaign_tomb_block(self._kills())
        assert "战役内墓穴" in block
        assert "[SCREEN]" in block and "Fast Conv Bound" in block
        assert "主张：we achieve X" in block
        assert "死因：x<0 counterexample" in block

    def test_empty_kills_returns_empty(self):
        assert campaign_tomb_block([]) == ""
        assert campaign_tomb_block([{"not": "a kill"}]) != ""  # 缺字段也不炸

    def test_prefers_kill_evidence_over_plain_reason(self):
        kills = [{"stage": "NOVELTY", "title": "T", "reason": "dup",
                  "kill_evidence": "paper #42 exact match"}]
        assert "paper #42 exact match" in campaign_tomb_block(kills)

    def test_char_cap_drops_with_marker(self):
        kills = [
            {"stage": "SCREEN", "title": f"T{i}", "reason": "r" * 120,
             "core_claim": "c" * 100}
            for i in range(50)
        ]
        block = campaign_tomb_block(kills, max_chars=1500)
        assert len(block) < 2000
        assert "省略" in block


# --------------------------------------------------------------------------- #
#  load_campaign_kills 文件回退
# --------------------------------------------------------------------------- #

class TestLoadCampaignKills:

    def test_reads_json_and_tolerates_missing_and_bad(self, tmp_path):
        camps = tmp_path / "camps"
        (camps / "c1" / "artifacts").mkdir(parents=True)
        (camps / "c1" / "artifacts" / "kills.json").write_text(
            json.dumps([{"stage": "NOVELTY", "title": "T"}]), encoding="utf-8")
        assert load_campaign_kills(camps, "c1")[0]["title"] == "T"
        assert load_campaign_kills(camps, "nope") == []
        (camps / "c2" / "artifacts").mkdir(parents=True)
        (camps / "c2" / "artifacts" / "kills.json").write_text("{bad json", encoding="utf-8")
        assert load_campaign_kills(camps, "c2") == []


# --------------------------------------------------------------------------- #
#  SEEK 注入（第二轮起语义：有 kills 才注入）
# --------------------------------------------------------------------------- #

SEEK_CONTENT = json.dumps({"ideas": [{
    "title": "Idea", "slug": "idea", "significance": 0.5,
    "win_odds": 0.5, "difficulty": 0.5, "rationale": "r",
}]})


class TestSeekTombInjection:

    def _run(self, monkeypatch, context):
        stage = SeekStage(llm=None, config=load_config())
        fake = FakeAgent(SEEK_CONTENT)
        monkeypatch.setattr(stage, "_make_agent_loop", lambda: fake)
        result = stage.run(_campaign(), context)
        return fake, result

    def test_no_kills_no_injection(self, monkeypatch):
        fake, _ = self._run(monkeypatch, StageContext(brief=_brief()))
        assert "战役内墓穴" not in fake.calls[0]["prompt"]

    def test_kills_in_extra_injected(self, monkeypatch):
        ctx = StageContext(brief=_brief())
        ctx.extra["kills"] = [{
            "stage": "SCREEN", "title": "Dead Idea",
            "core_claim": "claim", "kill_evidence": "ev",
        }]
        fake, _ = self._run(monkeypatch, ctx)
        assert "战役内墓穴" in fake.calls[0]["prompt"]
        assert "Dead Idea" in fake.calls[0]["prompt"]

    def test_file_fallback_when_extra_empty(self, monkeypatch, tmp_path):
        cfg = Config(storage=StorageConfig(
            db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "camps")))
        stage = SeekStage(llm=None, config=cfg)
        fake = FakeAgent(SEEK_CONTENT)
        monkeypatch.setattr(stage, "_make_agent_loop", lambda: fake)
        camp = _campaign()
        art = tmp_path / "camps" / camp.id / "artifacts"
        art.mkdir(parents=True)
        (art / "kills.json").write_text(
            json.dumps([{"stage": "GRADE", "title": "FileDead"}]), encoding="utf-8")
        stage.run(camp, StageContext(brief=_brief()))
        assert "FileDead" in fake.calls[0]["prompt"]


# --------------------------------------------------------------------------- #
#  记忆页转写携带 core_claim
# --------------------------------------------------------------------------- #

class TestMemoryPageCoreClaim:

    def test_idea_page_carries_core_claim(self):
        pages = idea_pages_from_campaign(
            "c1",
            [{"slug": "cand", "title": "T", "status": "dead"}],
            [{"slug": "cand", "stage": "NOVELTY",
              "core_claim": "we achieve X", "reason": "dup"}],
            origin_project="p1", origin_brief_title="B",
        )
        assert pages[0].status == "failed"
        assert pages[0].core_claim == "we achieve X"
        assert "核心主张" in pages[0].to_page()

    def test_page_roundtrip(self, tmp_path):
        store = MemoryStore(tmp_path)
        pages = idea_pages_from_campaign(
            "c1",
            [{"slug": "cand", "title": "T", "status": "dead"}],
            [{"slug": "cand", "stage": "NOVELTY",
              "core_claim": "we achieve X", "reason": "dup"}],
            origin_project="p1", origin_brief_title="B",
        )
        store.record_batch(pages)
        loaded = store.list_ideas()
        assert any(i.core_claim == "we achieve X" for i in loaded)
