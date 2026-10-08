"""GRADE — rate the verified candidate loophole / trivial / thin / solid.

Default is **solid**; when unsure, do NOT downgrade (HM-Pro burned real,
publishable results by grading on gut feel). "Assembling known tools" is not a
reason to downgrade — assembling them into a correct new bound/characterization
IS solid. Only loophole (the claim is vacuous) and trivial (nobody would care)
kill the candidate; thin and solid both proceed to WRITE.

GRADE is pure judgement: read-only, no network, no execution.
"""

from __future__ import annotations

from haa.models import GradeVerdict
from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult

# Lenient mapping so a slightly-off label still resolves to a real verdict.
_VERDICT_MAP = {
    "SOLID": GradeVerdict.SOLID,
    "THIN": GradeVerdict.THIN,
    "TRIVIAL": GradeVerdict.TRIVIAL,
    "LOOPHOLE": GradeVerdict.LOOPHOLE,
}


class GradeStage(BaseStage):
    """Rate the candidate loophole/trivial/thin/solid (default: solid)."""

    name = "GRADE"
    allowed_tools = {"read_file", "calculator"}  # pure judgement, read-only + verify arithmetic

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="GRADE: no active candidate")
        anchor, _aid, anchored = self._anchor(context)
        if anchored:
            # 行为矩阵：GRADE → 评证据强度（证明与实验支撑是否扎实）
            prompt = render_prompt(
                "grade_anchor", candidate=cand, anchor=anchor, brief=context.brief
            ) + self._brief_block(context.brief) + self._anchor_guard_clause()
        else:
            prompt = render_prompt(
                "grade",
                candidate=cand,
                design=context.design,
                verify_findings=context.verify_findings,
                verify_passed=context.verify_passed,
            ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(5),
        )
        data = self._parse_json(result.content)
        label = str(data.get("grade", "")).strip().upper()
        # Default to SOLID when unsure (HM-Pro: don't downgrade on gut feel).
        verdict = _VERDICT_MAP.get(label, GradeVerdict.SOLID)
        cand.grade = verdict
        # P1-c 分型标注（轻量确定性判定，v2 换 LLM）
        from haa.branch_duel import classify_paper_type
        paper_type = classify_paper_type(
            cand.title, cand.rationale, cand.positive_claim)
        out = {
            "grade": verdict.value,
            "rationale": str(data.get("rationale", "")),
            "paper_type": paper_type,
            "anchor_diff": data.get("anchor_diff"),
            "trace": result.messages,
        }
        if verdict in (GradeVerdict.TRIVIAL, GradeVerdict.LOOPHOLE):
            return StageResult.abort_candidate(reason=f"grade: {verdict.value}", **out)
        return StageResult.continue_(**out)
