"""DESIGN — design a proof / experiment plan, offline.

HM-Pro Lesson 1 (rework must carry context): when VERIFY sends a candidate back
here, ``context.verify_findings`` carries the counterexamples found last round
and they are rendered into the prompt so DESIGN addresses each one instead of
re-deriving blind.

**Offline by design**: ``allowed_tools`` has NO web tools — DESIGN must reason,
not assemble existing proofs from the web. Only read/write of the campaign dir.
"""

from __future__ import annotations

from haa.prompts import render_prompt
import logging as _logging
_logger = _logging.getLogger(__name__)
from haa.stages.base import BaseStage, StageResult


class DesignStage(BaseStage):
    """Design the plan; on rework, consume the prior verify findings."""

    name = "DESIGN"
    allowed_tools = {"read_file", "write_file", "calculator", "edit_file", "to_do_write"}  # offline: no web

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="DESIGN: no active candidate")
        prompt = render_prompt(
            "design",
            candidate=cand,
            design=context.design,
            verify_findings=context.verify_findings,
            design_round=context.design_round,
            brief=context.brief,
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(50),
        )
        data = self._parse_json(result.content)
        data["trace"] = result.messages
        # P1-b 概念档案：解析+校验+存入 context.extra
        raw_concepts = data.get("concepts") or []
        from haa.concept_archive import validate_concepts
        cards, concept_errors = validate_concepts(raw_concepts)
        if concept_errors:
            _logger.warning("concept archive validation errors: %s", concept_errors[:3])
            data["concept_errors"] = concept_errors  # 喂回模型（下轮 DESIGN 可修复）
        context.extra["concepts"] = [c.model_dump() for c in cards]
        context.extra["section_concepts"] = data.get("section_concepts") or {}

        return StageResult.continue_(**data)
