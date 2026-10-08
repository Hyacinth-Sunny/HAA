"""PILOT —— 先导实验微阶段（大修第三章 §8.2，M2 批次交付）。

位置：EXP_FEASIBILITY 通过之后、人工关卡之前（特性开关
``harness.features.pilot`` 默认关——Ch6 纪律，开启后接入）。
流程：LLM agent 在 campaign 沙箱 pilot/ 下生成 solve.sh 先导方案 →
``run_experiment`` 执行（容器/轻量沙箱）→ 四路判决（supported /
partially / not_supported / inconclusive）。

判决语义（代码不信自报，从 verdict 枚举机械映射）：
- supported / partially → 人工关卡（进 WRITE 前的既有关口）；
- not_supported → 候选判死（死因入 kills，供墓穴与 ARV 复核；top-k
  分支回退在 P1-c 批次接入后改为取次优分支）；
- inconclusive → 人工关卡并携带 inconclusive 标记（人裁）。
"""

from __future__ import annotations

import logging

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult, StageStatus

logger = logging.getLogger("haa.stage.pilot")

VERDICTS = ("supported", "partially", "not_supported", "inconclusive")


class PilotStage(BaseStage):
    """Run a small-scale pilot experiment and judge it (four-way verdict)."""

    name = "PILOT"
    allowed_tools = {"read_file", "write_file", "run_experiment", "budget_status"}

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="PILOT: no active candidate")
        prompt = (
            render_prompt(
                "pilot",
                candidate=cand,
                exp_spec=context.extra.get("exp_spec"),
                brief=context.brief,
            )
            + self._brief_block(context.brief)
        )
        anchor, _aid, anchored = self._anchor(context)
        if anchored:
            prompt += self._anchor_guard_clause()
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(12),
        )
        data = self._parse_json(result.content)
        verdict = str(data.get("verdict", "inconclusive")).strip().lower()
        if verdict not in VERDICTS:
            verdict = "inconclusive"
        out = {
            "verdict": verdict,
            "metrics_seen": data.get("metrics_seen", {}),
            "evidence": str(data.get("evidence", "")),
            "anchor_diff": data.get("anchor_diff"),
            "trace": result.messages,
        }
        if verdict == "not_supported":
            return StageResult.abort_candidate(
                reason="pilot: not_supported", **out)
        return StageResult.continue_(**out)
