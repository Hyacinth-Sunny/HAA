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
