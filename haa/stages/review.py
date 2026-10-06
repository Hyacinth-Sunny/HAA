"""REVIEW — three-way panel, physically blinded (philosophy 3).

Three independent reviewers — correctness / quality / industry — each gets a
copy of the paper with peer reviews and any self-assessment STRIPPED OUT, so no
reviewer can see another. Each returns a score in [0,1]; the mean is the
``overall`` used for the Lesson-3 snapshot logic, and ``decision``
(accept ≥ threshold else reject) drives the REVIEW⇄REFINE loop.

The per-lens prompt template is the reviewer's instructions (system prompt); the
blinded paper text is the user prompt the reviewer actually reads.
Read-only stage: a reviewer can read but never modify the paper.
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages._common import SECTION_RENDER_ORDER
from haa.stages._common import clamp01
from haa.stages.base import BaseStage, StageResult

# Average score at/above which the panel accepts. v1.0.5 起可由
# ``pipeline.review_accept_threshold`` 覆盖（smoke5/6 的 0.667/0.675 死在
# 硬编码 0.7 上——三轮 REFINE 空转后照发 degraded 的"差强人意带"剧场）。
ACCEPT_THRESHOLD = 0.7

# Keys that must NOT leak into a reviewer's copy (physical blinding, philosophy 3).
_BLINDED_KEYS = frozenset(
    {"review", "reviews", "self_assessment", "self_assessment_score", "trace", "self_negation_scan", "addressed"}
)

_SECTION_ORDER = SECTION_RENDER_ORDER


class ReviewStage(BaseStage):
    """Three physically-blinded reviews → aggregate decision + overall score."""

    name = "REVIEW"
    allowed_tools = {"read_file", "multi_agents"}  # read-only + parallel panel

    # (lens name, prompt template name) — templates are pure instructions.
    # v1.0.3 增设 fidelity lens：审"论文是否兑现简报委托"（smoke5 实证——
    # 内部连贯的论文可以整个做错题目，其余 lens 全部高分放行）。
    lenses = (
        ("correctness", "review/correctness"),
        ("quality", "review/quality"),
        ("industry", "review/industry"),
        ("fidelity", "review/fidelity"),
    )

    def run(self, campaign, context):  # noqa: D401
        blinded = self._blind(context.paper or {})
        # fidelity lens 需要简报铁律块作为评审依据，直接进论文文本（各 lens
        # 物理致盲互不可见意见——铁律块是评审输入不是意见，无污染）。
        paper_text = self._paper_text(blinded)
        brief_block = self._brief_block(context.brief)
        review_text = (
            paper_text + brief_block if brief_block else paper_text
        )
        agent = self._make_agent_loop()
        reports: dict = {}
        scores: dict = {}
        for lens, prompt_name in self.lenses:
            system_prompt = render_prompt(
                prompt_name,
                brief_block=brief_block,
            )
            res = agent.run(
                review_text,
                system_prompt=system_prompt,
                stage_name=self.name,
                campaign_id=campaign.id,
                json_mode=True,
                max_tool_calls=self._tool_limit(2),
            )
            data = self._parse_json(res.content)
            reports[lens] = {
                "score": clamp01(data.get("score")),
                "verdict": str(data.get("verdict", "")),
                "major_issues": data.get("major_issues", []) or [],
                "minor_issues": data.get("minor_issues", []) or [],
            }
            scores[lens] = reports[lens]["score"]

        overall = sum(scores.values()) / len(scores) if scores else 0.0
        threshold = ACCEPT_THRESHOLD
        if self.config is not None:
            threshold = float(
                getattr(self.config.pipeline, "review_accept_threshold", ACCEPT_THRESHOLD)
            )
        decision = "accept" if overall >= threshold else "reject"
        return StageResult.continue_(
            decision=decision,
            overall=overall,
            scores=scores,
            reports=reports,
        )

    @staticmethod
    def _blind(paper: dict) -> dict:
        """Strip peer-review / self-assessment / trace keys → blinded copy."""
        return {k: v for k, v in paper.items() if k not in _BLINDED_KEYS}

    @staticmethod
    def _paper_text(paper: dict) -> str:
        parts = [f"{sec.upper()}\n{paper.get(sec, '')}" for sec in _SECTION_ORDER if paper.get(sec)]
        return "\n\n".join(parts) if parts else "(empty paper)"
