"""REFINE — revise the paper upward from review feedback (Lesson 3).

HM-Pro Lesson 3 (regression has no floor): BEFORE refining, if the latest review
scored WORSE than the best snapshot, roll back to the best snapshot first — then
add substance. Refining is additive (fill gaps the reviewers flagged, add the
negative result, clarify), never a slim-down. Always returns to REVIEW; the loop
ceiling (not this stage) decides when to stop.
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult

_SECTIONS = ("abstract", "intro", "method", "eval", "related", "conclusion")


class RefineStage(BaseStage):
    """Roll back to best if we regressed (Lesson 3), then refine upward."""

    name = "REFINE"
    allowed_tools = {"read_file", "write_file", "edit_file", "to_do_write"}

    def run(self, campaign, context):  # noqa: D401
        # HM-Pro Lesson 3: never let a round sink below the best version.
        self._maybe_rollback(context)

        prompt = (
            render_prompt("refine", paper=context.paper, review=context.review)
            + self._brief_block(context.brief)
        )
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(3),
        )
        data = self._parse_json(result.content)

        # Start from the (possibly rolled-back) paper; overlay revised sections.
        refined = dict(context.paper) if isinstance(context.paper, dict) else {}
        for sec in _SECTIONS + ("title",):
            value = data.get(sec)
            if value:
                refined[sec] = str(value)
        refined["addressed"] = data.get("addressed", []) or []
        refined["self_negation_scan"] = data.get("self_negation_scan", []) or []
        refined["trace"] = result.messages
        return StageResult.continue_(**refined)

    @staticmethod
    def _maybe_rollback(context) -> None:
        """Restore the best snapshot if the latest review regressed (Lesson 3)."""
        best = context.best_snapshot
        if not best:
            return
        current = (context.review or {}).get("overall")
        best_overall = (best.get("review") or {}).get("overall")
        if (
            current is not None
            and best_overall is not None
            and current < best_overall
        ):
            context.restore_paper(best)
