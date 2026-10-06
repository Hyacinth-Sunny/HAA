"""EXP_FEASIBILITY — 对抗式检验实验规格的合理性。

类比 VERIFY 对 DESIGN 的角色：寻找实验设计中的漏洞。
5 个检验维度：数据可行性、基线公平性、指标完整性、资源可行性、文献一致性。

Verdict 由 pipeline 硬编码推导（同 Lesson 2：不信任 self-report，从 blockers
列表机械推导）——本 stage 只产出 blockers 列表，PASS/REWORK/ADJUSTABLE 等判
决在 ``Pipeline._after_exp_feasibility`` 里。
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages.base import BaseStage, StageResult


class ExpFeasibilityStage(BaseStage):
    """检验实验规格的合理性，产出 blockers 列表。verdict 由 pipeline 推导。"""

    name = "EXP_FEASIBILITY"
    allowed_tools = {
        "web_search", "web_fetch", "search_paper",
        "read_file", "calculator", "to_do_write",
    }
    # 注意：不允许 write_file/edit_file — 这是只读检验阶段。

    def run(self, campaign, context):  # noqa: D401
        exp_spec = context.extra.get("exp_spec", {})
        if not exp_spec:
            return StageResult.abort_campaign(
                reason="EXP_FEASIBILITY: no exp_spec to check"
            )

        prompt = render_prompt(
            "exp_feasibility",
            candidate=context.candidate,
            design=context.design,
            exp_spec=exp_spec,
            exp_round=context.exp_round,
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(8),
        )
        data = self._parse_json(result.content)

        # 提取 blockers（不信任 LLM 的 verdict self-report）
        blockers = data.get("blockers", []) or []
        normalized_blockers = []
        for b in blockers:
            if not isinstance(b, dict):
                continue
            severity = str(b.get("severity", "major")).strip().lower()
            if severity not in ("fatal", "major", "minor"):
                severity = "major"
            normalized_blockers.append({
                "severity": severity,
                "category": str(b.get("category", "unknown")).strip().lower(),
                "detail": str(b.get("detail", "")),
                "evidence": str(b.get("evidence", "")),
                "fix_suggestion": str(b.get("fix_suggestion", "")),
            })

        return StageResult.continue_(
            blockers=normalized_blockers,
            assessment=str(data.get("overall_assessment", "")),
            trace=result.messages,
        )
