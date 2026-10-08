"""SEEK — turn a Brief into an ordered queue of candidate paper ideas.

HM-Pro philosophy 5 (close the browser and think first): SEEK reasons
internally *before* any web-based dedup, so the ideas are not just incremental
variants of existing papers' future-work sections. The prompt instructs the
model to list 15–20 open questions from its own knowledge first, THEN use the
search/dedup tools. It emits ``seek_candidate_count`` candidates; together they
form the queue (Lesson 4: dead candidates advance it).
"""

from __future__ import annotations

from pathlib import Path

from haa.models import Candidate, CandidateStatus
from haa.prompts import render_prompt
from haa.stages._common import clamp01, slugify
from haa.stages.base import BaseStage, StageResult


def _knowledge_basenames(brief) -> list[str]:
    """knowledge_files 的文件名列表（staging 后都在 knowledge/ 下）。"""
    return [Path(k).name for k in getattr(brief, "knowledge_files", None) or []]


class SeekStage(BaseStage):
    """Generate candidate ideas from the brief (think first, then dedup online)."""

    name = "SEEK"
    allowed_tools = {"web_search", "web_fetch", "search_paper", "read_file", "multi_agents"}

    def run(self, campaign, context):  # noqa: D401
        brief = context.brief
        n = (
            self.config.pipeline.seek_candidate_count
            if self.config is not None
            else 5
        )
        anchor, anchor_idea_id, anchored = self._anchor(context)
        diverge_on = self.config is None or self.config.harness.feature("divergence")
        if diverge_on and not anchored and n > 1:
            return self._run_divergence(campaign, context, brief, n)

        if anchored:
            # 行为矩阵（第三章 §4.2）：SEEK 降级为锚点细化——单候选、不发散
            prompt = (
                render_prompt(
                    "seek_anchor",
                    brief=brief,
                    anchor=anchor,
                    anchor_idea_id=anchor_idea_id,
                )
                + self._brief_block(brief)
                + self._anchor_guard_clause()
            )
        else:
            prompt = (
                render_prompt(
                    "seek",
                    brief=brief,
                    knowledge_basenames=_knowledge_basenames(brief),
                    candidate_count=n,
                )
                + self._memory_brief_suffix(brief)
                + self._campaign_tomb_block(campaign, context)  # 批次2：战役内死路清单（第二轮起非空）
            )
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(10),
        )
        data = self._parse_json(result.content)
        raw_ideas = data.get("ideas") or data.get("candidates") or []
        candidates = [
            self._to_candidate(idea, campaign.id, idx)
            for idx, idea in enumerate(raw_ideas[:n])
            if isinstance(idea, dict)
        ]
        if not candidates:
            return StageResult.abort_campaign(reason="SEEK produced no candidates")
        return StageResult.continue_(
            candidates=candidates,
            open_questions=data.get("open_questions", []),
            trace=result.messages,
        )

    def _run_divergence(self, campaign, context, brief, n):
        """发散-收敛三步（第三章 §5）：①k 个正交方向 → ②每方向候选 → ③聚类去重。"""
        k = 5
        if self.config is not None:
            k = int(getattr(self.config.pipeline, "divergence_k", 5) or 5)
        # 步骤 1：生成 k 个正交方向
        result1 = self._run_agent(
            self._render_diverge_prompt(brief, k),
            stage_name=self.name, campaign_id=campaign.id,
            json_mode=True, max_tool_calls=2,
        )
        data1 = self._parse_json(result1.content)
        directions = [d for d in (data1.get("directions") or [])
                      if isinstance(d, dict) and d.get("name")][:k]
        if not directions:
            directions = [{"name": "default", "core_conflict": "",
                           "mechanism_hint": "", "risk_level": "balanced"}]
        # 步骤 2：每方向独立生成候选
        all_candidates = []
        per_dir = max(1, n // max(len(directions), 1))
        for direction in directions:
            result2 = self._run_agent(
                render_prompt("seek_direction", brief=brief,
                              direction=direction, n=per_dir)
                + self._brief_block(brief)
                + self._campaign_tomb_block(campaign, context),
                stage_name=self.name, campaign_id=campaign.id,
                json_mode=True, max_tool_calls=self._tool_limit(10),
            )
            data2 = self._parse_json(result2.content)
            raw_ideas = data2.get("ideas") or data2.get("candidates") or []
            for idx, idea in enumerate(raw_ideas[:per_dir]):
                if isinstance(idea, dict):
                    idea["_direction"] = direction.get("name", "")
                    all_candidates.append(idea)
        # 步骤 3：聚类去重（v1 确定性：同 direction 内 title 相似>0.7 去重）
        deduped = self._cluster_dedup(all_candidates)
        candidates = [
            self._to_candidate(idea, campaign.id, idx)
            for idx, idea in enumerate(deduped[:n])
            if isinstance(idea, dict)
        ]
        if not candidates:
            return StageResult.abort_campaign(
                reason="seek: divergence produced no candidates")
        context.candidates = candidates
        context.extra["divergence_directions"] = [
            d.get("name") for d in directions]
        return StageResult.continue_(
            candidates=candidates,
            directions=[d.get("name") for d in directions],
        )

    def _render_diverge_prompt(self, brief, k):
        from haa.prompts import render_prompt as _rp
        return _rp("seek_diverge", brief=brief, k=k) + self._brief_block(brief) \
            + self._campaign_tomb_block(None, context=None) if False else \
            _rp("seek_diverge", brief=brief, k=k) + self._brief_block(brief)

    @staticmethod
    def _cluster_dedup(ideas, threshold=0.7):
        """轻量去重：同方向内 title 相似度>阈值只留首个（v1 确定性；v2 换 LLM）。"""
        import difflib
        kept: list[dict] = []
        for idea in ideas:
            title = str(idea.get("title", ""))
            dup = False
            for existing in kept:
                if existing.get("_direction") == idea.get("_direction"):
                    ratio = difflib.SequenceMatcher(
                        None, title, str(existing.get("title", ""))).ratio()
                    if ratio > threshold:
                        dup = True
                        break
            if not dup:
                kept.append(idea)
        return kept

    @staticmethod
    def _to_candidate(idea: dict, campaign_id: str, idx: int) -> Candidate:
        title = str(idea.get("title") or f"Idea {idx + 1}").strip() or f"Idea {idx + 1}"
        return Candidate(
            campaign_id=campaign_id,
            slug=slugify(idea.get("slug") or title),
            title=title,
            significance=clamp01(idea.get("significance"), 0.5),
            win_odds=clamp01(idea.get("win_odds"), 0.5),
            difficulty=clamp01(idea.get("difficulty"), 0.5),
            rationale=str(idea.get("rationale", "")),
            positive_claim=str(idea.get("positive_claim", "")),
            negative_claim=str(idea.get("negative_claim", "")),
            attack_plan=str(idea.get("attack_plan", "")),
            closest_prior_work=str(idea.get("closest_prior_work", "")),
            queue_index=idx,
            status=CandidateStatus.PROPOSED,
        )
