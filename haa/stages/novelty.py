"""NOVELTY — is this candidate actually new?

Uses the search/paper tools (multi-angle, with recent years) to check whether
the candidate's claim has already been solved. Verdicts: NEW / INSUFFICIENT /
SOLVED. SOLVED requires a concrete citation — vague "someone probably did this"
is not enough, and the default when evidence is thin is to let the candidate
through (killing a real idea at the first gate is the most expensive mistake).
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult


class NoveltyStage(BaseStage):
    """Judge the active candidate NEW / INSUFFICIENT / SOLVED (online dedup)."""

    name = "NOVELTY"
    allowed_tools = {"web_search", "web_fetch", "search_paper", "multi_agents"}

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="NOVELTY: no active candidate")
        anchor, _aid, anchored = self._anchor(context)
        if anchored:
            # 行为矩阵：NOVELTY → 先例碰撞检查（迁移不算撞车规则在模板内）
            prompt = render_prompt(
                "novelty_anchor", brief=context.brief, candidate=cand, anchor=anchor
            ) + self._brief_block(context.brief) + self._anchor_guard_clause()
        else:
            prompt = render_prompt(
                "novelty", brief=context.brief, candidate=cand
            ) + self._brief_block(context.brief)
        if (
            self.config is not None
            and self.config.memory.enabled
            and self.config.memory.inject_into_novelty
        ):
            prompt += self._memory_brief_suffix(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(8),
        )
        data = self._parse_json(result.content)
        verdict = str(data.get("verdict", "NEW")).strip().upper() or "NEW"
        out = {
            "verdict": verdict,
            "rationale": str(data.get("rationale", "")),
            "closest_prior_work": str(data.get("closest_prior_work", "")),
            "search_queries_used": data.get("search_queries_used", []),
            "precedents": data.get("precedents", []),
            "anchor_diff": data.get("anchor_diff"),
            "trace": result.messages,
        }
        # Record dedup findings on the candidate so later stages see them.
        if out["closest_prior_work"] and out["closest_prior_work"].lower() not in ("none", ""):
            cand.closest_prior_work = out["closest_prior_work"]
        if verdict == "SOLVED":
            return StageResult.abort_candidate(reason="novelty: SOLVED", **out)
        return StageResult.continue_(**out)
