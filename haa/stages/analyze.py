"""ANALYZE 微阶段（批次18 挂点5，修订计划书05 §3.1）。

位置：实验完成后、WRITE 前。仿 PILOT 微阶段模式，features.analyze 默认关。
产出 analysis.json（每条主张的指标证据+效应量+反例+局限）。
观点类终审在此落锤：指标反主张 → kill_reason="viewpoint" → 判死入墓穴。
"""
from __future__ import annotations

import logging

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult

logger = logging.getLogger("haa.stage.analyze")


class AnalyzeStage(BaseStage):
    """P2 ANALYZE：分析实验结果，观点终审。"""

    name = "ANALYZE"
    allowed_tools = {"read_file", "calculator"}

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        if cand is None:
            return StageResult.abort_campaign(reason="ANALYZE: no active candidate")

        exp_spec = context.extra.get("exp_spec") or {}
        metrics = context.extra.get("pilot_metrics") or {}
        debug_log = context.extra.get("debug_log", "")

        prompt = (
            render_prompt(
                "analyze",
                candidate=cand,
                exp_spec=exp_spec,
                metrics=metrics,
                debug_log=debug_log[-2000:],
                brief=context.brief,
            )
            + self._brief_block(context.brief)
        )
        result = self._run_agent(
            prompt, stage_name=self.name, campaign_id=campaign.id,
            json_mode=True, max_tool_calls=self._tool_limit(5),
        )
        data = self._parse_json(result.content)

        # 观点终审（§3.1）：指标反主张 → 判死
        viewpoint_verdict = str(data.get("viewpoint_verdict", "")).strip().lower()
        out = {
            "analysis": data.get("analysis", {}),
            "viewpoint_verdict": viewpoint_verdict,
            "claims_supported": data.get("claims_supported", []),
            "claims_unsupported": data.get("claims_unsupported", []),
            "figures_suggested": data.get("figures_suggested", []),
            "trace": result.messages,
        }
        if viewpoint_verdict == "viewpoint_unsupported":
            return StageResult.abort_candidate(
                reason="viewpoint", **{k: v for k, v in out.items() if k != "trace"})
        return StageResult.continue_(**out)
