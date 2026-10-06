"""EXP_SPEC — 设计具体实验规格。

把 GRADE 通过的 candidate 展开为具体的、可执行的实验规格。
**联网阶段**（需要检索领域文献以参考实验范式、核实数据集可获取性）。

返工时（exp_round > 0）携带上一轮 EXP_FEASIBILITY 的 blockers，
必须逐条回应（同 Lesson 1：返工不能失忆）。
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult


class ExpSpecStage(BaseStage):
    """设计具体实验规格：数据集、基线、指标、协议、消融、资源估算。"""

    name = "EXP_SPEC"
    allowed_tools = {
        "web_search", "web_fetch", "search_paper",
        "read_file", "write_file", "calculator", "to_do_write",
    }

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="EXP_SPEC: no active candidate")

        exp_round = context.exp_round
        exp_findings = context.extra.get("exp_findings", [])

        prompt = render_prompt(
            "exp_spec",
            candidate=cand,
            design=context.design,
            verify_findings=context.verify_findings,
            brief=context.brief,
            exp_round=exp_round,
            exp_findings=exp_findings,
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(10),
        )
        data = self._parse_json(result.content)
        data["trace"] = result.messages
        return StageResult.continue_(**data)
