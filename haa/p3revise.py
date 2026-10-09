"""P3 修订核心件（批次17，修订计划书05 §3）。

ANALYZE 微阶段 / WRITE 六节 schema / 简报锚定 / 可读性 lint。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("haa.p3revise")

# ============================================================================ #
#  §3.2 WRITE 六节 schema
# ============================================================================ #

PAPER_SECTIONS_V2 = ("abstract", "intro", "background", "method",
                     "results", "conclusion")
NO_EXPERIMENT_PLACEHOLDER = "(no-experiment: 本节需实验数据，尚未产出)"

SECTION_RENDER_ORDER_V2 = PAPER_SECTIONS_V2 + ("eval", "related")


def validate_paper_sections(paper: dict) -> list[str]:
    """六节 schema 校验——缺失节需标注 no-experiment 占位而非空串。"""
    errors: list[str] = []
    for sec in PAPER_SECTIONS_V2:
        val = paper.get(sec)
        if val is None:
            errors.append(f"论文缺「{sec}」节")
        elif isinstance(val, str) and not val.strip() and sec in ("results", "conclusion"):
            errors.append(f"「{sec}」节为空串——应标注 no-experiment 占位")
    return errors


def normalize_paper_v2(paper: dict) -> dict:
    """四节→六节迁移：results/conclusion 缺失时填 no-experiment 占位。"""
    out = dict(paper)
    for sec in ("results", "conclusion"):
        if not (out.get(sec) or "").strip():
            out[sec] = NO_EXPERIMENT_PLACEHOLDER
    return out


# ============================================================================ #
#  §3.3 简报锚定（ANALYZE/WRITE 提示词携带 brief_block）
# ============================================================================ #

ANALYZE_BRIEF_CLAUSE = """
⛓⛓⛓ 简报锚定纪律 ⛓⛓⛓
分析结论与图表建议必须对照研究简报的约束与产出要求——不能跑出简报边界。
每张图必须挂"实验三问"之一（验证什么/怎么验证/预期结果）。
"""


# ============================================================================ #
#  §3.4 可读性 lint（确定性层）
# ============================================================================ #

# 句长上限
MAX_CN_CHARS = 60
MAX_EN_WORDS = 40
# 缩略语首现必须定义
_ABBREV_PATTERN = re.compile(r"\b([A-Z]{2,8})\b")
# 术语密度（每百字允许的最大未解释术语数）
MAX_TERM_DENSITY = 5


def lint_readability(text: str) -> list[dict]:
    """确定性可读性检查（§3.4——lint 先行，润色模块挂账）。"""
    issues: list[dict] = []

    # 1. 句长检查
    sentences = re.split(r"[。！？.!?\n]+", text)
    for i, sent in enumerate(sentences):
        sent = sent.strip()
        if not sent:
            continue
        cn_chars = len(re.findall(r"[\u4e00-\u9fff]", sent))
        en_words = len(re.findall(r"[a-zA-Z]+", sent))
        if cn_chars > MAX_CN_CHARS:
            issues.append({"type": "long_sentence", "line": i + 1,
                           "detail": f"中文句长 {cn_chars} 字（上限 {MAX_CN_CHARS}）",
                           "excerpt": sent[:60] + "…"})
        elif en_words > MAX_EN_WORDS:
            issues.append({"type": "long_sentence", "line": i + 1,
                           "detail": f"英文句长 {en_words} 词（上限 {MAX_EN_WORDS}）",
                           "excerpt": sent[:60] + "…"})

    # 2. 缩略语首现定义
    seen_abbrevs: set[str] = set()
    for i, sent in enumerate(sentences):
        for m in _ABBREV_PATTERN.finditer(sent):
            abbrev = m.group(1)
            if abbrev not in seen_abbrevs:
                # 检查是否有定义（紧跟括号或 "stands for" 等）
                context = sent[m.end():m.end() + 40]
                has_def = bool(re.match(r"\s*[（(]", context) or
                               re.search(rf"{abbrev}.*?(?:stands for|是指|代表)",
                                         sent))
                if not has_def and abbrev not in ("DNA", "API", "CPU", "GPU",
                                                   "RAM", "SSD", "HTTP", "XML"):
                    issues.append({"type": "undefined_abbrev", "line": i + 1,
                                   "detail": f"缩略语 {abbrev} 首次出现未定义",
                                   "excerpt": sent[:50] + "…"})
                seen_abbrevs.add(abbrev)

    # 3. 术语密度（粗略——连续段落中高密度专业术语）
    # 此项保守，只检测极端情况
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for i, para in enumerate(paragraphs):
        if len(para) < 100:
            continue
        abbrevs = _ABBREV_PATTERN.findall(para)
        if len(abbrevs) > MAX_TERM_DENSITY * (len(para) / 100):
            issues.append({"type": "high_term_density", "line": i + 1,
                           "detail": f"段落术语密度过高（{len(abbrevs)} 个缩略语/段落）",
                           "excerpt": para[:60] + "…"})

    return issues


def render_lint_report(issues: list[dict]) -> str:
    if not issues:
        return "✓ 可读性检查通过"
    lines = [f"可读性检查：{len(issues)} 项问题"]
    for issue in issues[:10]:
        lines.append(f"  [{issue['type']}] L{issue['line']}: {issue['detail']}")
        if issue.get("excerpt"):
            lines.append(f"    → {issue['excerpt']}")
    return "\n".join(lines)
