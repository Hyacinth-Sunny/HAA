"""SCREEN — first kill, then maybe believe.

One-shot attempt to kill the candidate. Three legal kills: an explicit
counterexample (run it with exec_bash), a cited impossibility/bound, or an
already-solved paper. **No hard evidence ⇒ must let it through** — killing a
real idea here is the most expensive error in the whole pipeline.
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult


class ScreenStage(BaseStage):
    """Try to falsify the candidate; survive unless there is hard evidence."""

    name = "SCREEN"
    allowed_tools = {"exec_bash", "read_file", "write_file", "web_search", "calculator", "edit_file", "multi_agents", "to_do_write"}

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="SCREEN: no active candidate")
        prompt = render_prompt(
            "screen", candidate=cand
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(5),
        )
        data = self._parse_json(result.content)
        survives = bool(data.get("survives", True))
        kill_method = str(data.get("kill_method", "none"))
        out = {
            "proposition": str(data.get("proposition", "")),
            "separating_instance": str(data.get("separating_instance", "")),
            "kill_method": kill_method,
            "evidence": str(data.get("evidence", "")),
            "survives": survives,
            "rationale": str(data.get("rationale", "")),
            "trace": result.messages,
        }
        if not survives:
            # A kill must carry real evidence; otherwise flip back to survive.
            has_evidence = bool(out["evidence"]) and kill_method in (
                "explicit_counterexample",
                "impossibility_reference",
                "already_solved",
            )
            if not has_evidence:
                out["survives"] = True
                out["rationale"] = (
                    "(auto-overridden: kill without hard evidence → survive) "
                    + out["rationale"]
                )
            else:
                return StageResult.abort_candidate(
                    reason=f"screen: {kill_method}", **out
                )
        return StageResult.continue_(**out)
