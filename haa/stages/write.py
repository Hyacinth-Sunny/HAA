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
        ) + self._brief_block(context.brief) + self._concept_slice_block(context)
        result = self._run_agent(
            prompt,
            stage_name=self.name,
            campaign_id=campaign.id,
            json_mode=True,
            max_tool_calls=self._tool_limit(8),
        )
        data = self._parse_json(result.content)
        # A2 批次18-2：六节捕获——results/conclusion 由 WRITE 提示词按取材
        # 规则产出（有 analysis 写实 / 无则占位串）；缺节仍由
        # normalize_paper_v2 兜底补占位（pipeline._validate_paper）。
        paper = {sec: str(data.get(sec, "")) for sec in
                 PAPER_SECTIONS + ("results", "conclusion")}
        paper["title"] = str(data.get("title", cand.title if cand else ""))
        paper["outline"] = data.get("outline", []) or []
        paper["self_negation_scan"] = data.get("self_negation_scan", []) or []
        paper["trace"] = result.messages
        return StageResult.continue_(**paper)

    def _concept_slice_block(self, context):
        """概念档案切片注入（P1-b WRITE 消费侧）。"""
        from haa.concept_archive import (ConceptCard, render_concept_block,
                                          section_slices)
        raw = (context.extra or {}).get("concepts") or []
        section_map = (context.extra or {}).get("section_concepts") or {}
        if not raw:
            return ""
        try:
            cards = [ConceptCard(**{k: v for k, v in c.items()
                                    if k in ConceptCard.model_fields})
                     for c in raw if isinstance(c, dict)]
        except Exception:  # noqa: BLE001
            cards = []
        if not cards:
            return ""
        # 按章节映射提取全部可能章节的切片（v1 注入全部概念，章节过滤 v2）
        return "\n\n" + render_concept_block(cards)
