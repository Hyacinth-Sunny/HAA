"""Small helpers shared across stages.

Kept out of :mod:`haa.stages.base` (which holds only the interface types) so the
base module stays import-light. Two helpers + one constant:

* :func:`slugify` — coerce free text into a filesystem-safe slug (the
  :class:`~haa.models.Candidate` model enforces a strict slug shape).
* :func:`clamp01` — force an LLM-supplied number into ``[0, 1]`` (significance /
  win-odds / difficulty / review scores all live there).
* :data:`ESTIMATE_COST` — a modest per-call cost estimate handed to
  :meth:`~haa.llm.client.LLMClient.call` so the budget gate is *live*
  (HM-Pro Lesson 5); the true cost is reconciled from token usage afterwards.
"""

from __future__ import annotations

import re
from typing import Any

# A small positive estimate so gated stages can actually trip the budget gate.
# ``record()`` true-ups this to the real token-derived cost after the call.
ESTIMATE_COST: float = 0.05

# 论文前体的章节模式（v1.0.5 论文前半部标准）：前体 = 正常论文的前半部
# （abstract/intro/background/method），除两点外与正常论文无别——①无实验/
# 结论/不足分析/参考文献；②MD 而非 LaTeX。实验设计不进正文，作为 exp_spec
# 工件独立交付（HM-Pro 16 包先例：预注册评测计划形态）。
PAPER_SECTIONS: tuple[str, ...] = ("abstract", "intro", "background", "method")

# 渲染序：旧前体（smoke4-6，六节模式）的 eval/related/conclusion 追加在尾
# 部——读侧按此序展示，写侧只产 PAPER_SECTIONS。
SECTION_RENDER_ORDER: tuple[str, ...] = (
    "abstract", "intro", "background", "method",
    "eval", "related", "conclusion",
)

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, non-alphanumeric → ``-``, trimmed; never empty.

    Fallback used when the LLM omits a slug or gives one that fails the
    candidate's strict slug validator.
    """
    s = _NON_SLUG.sub("-", (text or "").strip().lower()).strip("-")
    return s[:64] or "idea"


def clamp01(value: Any, default: float = 0.5) -> float:
    """Coerce ``value`` to a float in ``[0, 1]``; ``default`` if unparseable."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN guard
        return default
    return max(0.0, min(1.0, v))


def brief_blurb(brief: Any) -> str:
    """A compact one-block description of a Brief for prompts."""
    if brief is None:
        return "(no brief provided)"
    lines = [f"# {brief.title}", f"Problem area: {brief.problem_area}"]
    track = getattr(brief, "track", None)
    if track is not None:
        lines.append(f"Track: {track.value}")
    if getattr(brief, "constraints", None):
        lines.append("Constraints:\n- " + "\n- ".join(brief.constraints))
    if getattr(brief, "exclusions", None):
        lines.append("Avoid:\n- " + "\n- ".join(brief.exclusions))
    return "\n".join(lines)
