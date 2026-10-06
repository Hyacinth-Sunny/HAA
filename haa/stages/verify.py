"""VERIFY — search for a counterexample; PASS = "no counterexample found".

HM-Pro Lesson 2 (the unreachable bar): PASS is NOT "every obligation resolved"
— that bar can never be met. PASS is "in the model's reachable range, no
counterexample overturns the claim". The stage DERIVES ``verify_passed`` from
whether the model produced any counterexample, rather than trusting a
self-reported flag. Any counterexamples are returned as ``findings`` so the next
DESIGN round sees them (Lesson 1).
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult


class VerifyStage(BaseStage):
    """Hunt for a counterexample; PASS iff the list is empty (Lesson 2)."""

    name = "VERIFY"
    allowed_tools = {"web_search", "read_file", "exec_bash", "calculator", "to_do_write"}

    def run(self, campaign, context):  # noqa: D401
        design = context.design or {}
        if not design:
            return StageResult.abort_campaign(reason="VERIFY: no design to verify")
        prompt = render_prompt(
            "verify",
            candidate=context.candidate,
            design=design,
            verify_findings=context.verify_findings,
            design_round=context.design_round,
            verify_passed=context.verify_passed,
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(20),
        )
        data = self._parse_json(result.content)
        counterexamples = [
            str(c).strip()
            for c in (data.get("counterexamples") or [])
            if str(c).strip()
        ]
        # Lesson 2: PASS = "no counterexample found". Derived from the list,
        # not from any self-reported flag (which can never be satisfied).
        verify_passed = len(counterexamples) == 0
        findings = [
            {"kind": "counterexample", "detail": c} for c in counterexamples
        ]
        return StageResult(
            data={
                "verify_passed": verify_passed,
                "counterexamples": counterexamples,
                "notes": str(data.get("notes", "")),
                "trace": result.messages,
            },
            findings=findings,
        )
