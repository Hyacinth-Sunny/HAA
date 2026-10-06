"""WRITE — draft the FRONT HALF of a paper + sweep for self-defeating wording.

v1.0.5 论文前半部标准：abstract / intro / background / method 四节——除
①无实验/结论/limitations/参考文献 ②MD 而非 LaTeX 两点外，与正常论文体裁
无区别（教学见 prompts/write.md）。实验设计不进正文，由 EXP_SPEC 的
exp_spec 工件独立承载（HM-Pro 16 包先例：预注册评测计划）。写完后弹药
扫描自我弱化措辞（HM-Pro 教训）。
"""

from __future__ import annotations

from haa.prompts import render_prompt
from haa.stages._common import PAPER_SECTIONS
from haa.stages.base import BaseStage, StageResult


class WriteStage(BaseStage):
    """Draft all paper sections from the design + verify context."""

    name = "WRITE"
    allowed_tools = {"read_file", "write_file", "web_search", "web_fetch", "edit_file", "to_do_write"}

    def run(self, campaign, context):  # noqa: D401
        cand = context.candidate
        prompt = render_prompt(
            "write",
            candidate=cand,
            design=context.design,
            verify_passed=context.verify_passed,
            extra=context.extra,
            brief=context.brief,
        ) + self._brief_block(context.brief)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(8),
        )
        data = self._parse_json(result.content)
        paper = {sec: str(data.get(sec, "")) for sec in PAPER_SECTIONS}
        paper["title"] = str(data.get("title", cand.title if cand else ""))
        paper["outline"] = data.get("outline", []) or []
        paper["self_negation_scan"] = data.get("self_negation_scan", []) or []
        paper["trace"] = result.messages
        return StageResult.continue_(**paper)
