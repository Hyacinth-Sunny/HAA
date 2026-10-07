"""简报质量预检（大修第三章 §3.4，软门——只出报告不强制断流）。

三查：
1. **结构检查**：死格式三大核心部分（问题阐述/重难点分析/已有研究进展
   及其优劣势）的存在性与篇幅——缺失时提示"极可能被 Phase I 打回或导致
   后续 LLM 工作陷入混乱"（外框架原文语义）；
2. **矛盾检测**：一次轻量模型调用扫描简报内部矛盾（主张与判据冲突、
   范围与机制不一致、前后表述打架）——``contradiction_checker`` 可注入；
   未注入时跳过并在报告中注明（软门不因缺件断流）；
3. **模糊检测**：模糊词密度统计（"等""相关""若干""某种程度上"类）＋
   排他表述警告（"必须使用/只能采用"——简报宜松不宜死）。

输出报告（dict）：问题清单＋修改建议，供提交前修订或知情带病提交。
"""

from __future__ import annotations

import re
from typing import Any, Callable

VAGUE_WORDS = ("等等", "若干", "相关技术", "相关方法", "某种程度上",
               "一些", "比较好", "尽量", "适当", "等。", "等相关")
EXCLUSIVE_PATTERNS = (r"必须使用", r"只能采用", r"只能使用", r"必须采用",
                      r"唯一方案", r"不允许其他")
CORE_SECTIONS = (
    ("问题阐述", ("问题阐述", "研究问题", "问题描述")),
    ("重难点分析", ("重难点", "难点分析", "关键挑战")),
    ("已有研究进展", ("已有研究", "研究进展", "相关工作", "现有方案")),
)


def structure_check(md: str) -> list[dict]:
    problems = []
    for canonical, aliases in CORE_SECTIONS:
        hit = next((a for a in aliases if a in md), None)
        if hit is None:
            problems.append({
                "kind": "structure",
                "severity": "high",
                "message": f"缺少核心部分「{canonical}」——极可能被 Phase I 打回"
                           f"或导致后续 LLM 工作陷入混乱",
                "suggestion": f"补写「{canonical}」小节（对照模板 data/研究简报模板.md）",
            })
    return problems


def vagueness_check(md: str) -> list[dict]:
    hits = {w: md.count(w) for w in VAGUE_WORDS if w in md}
    problems = []
    if hits:
        top = sorted(hits.items(), key=lambda kv: -kv[1])[:5]
        problems.append({
            "kind": "vagueness",
            "severity": "medium",
            "message": "模糊词密度偏高：" + "、".join(f"{w}×{n}" for w, n in top),
            "suggestion": "把模糊表述换成可核查的具体对象/数量/判据",
        })
    for pat in EXCLUSIVE_PATTERNS:
        m = re.search(pat, md)
        if m:
            problems.append({
                "kind": "exclusive",
                "severity": "medium",
                "message": f"排他表述「{m.group(0)}」——简报宜松不宜死"
                           f"（给定实现路径会诱使模型凑向路径而非真推导）",
                "suggestion": "改为目标式表述（要求达成什么，而非指定唯一做法）",
            })
    return problems


def contradiction_check(md: str,
                        checker: Callable[[str], str] | None) -> list[dict]:
    """LLM 矛盾扫描（可注入；未注入时跳过并注明）。"""
    if checker is None:
        return [{
            "kind": "contradiction",
            "severity": "info",
            "message": "矛盾检测未运行（未注入轻量模型通道）",
            "suggestion": "（软门：不阻断提交；接入 checker 后自动补查）",
        }]
    try:
        answer = (checker(md) or "").strip()
    except Exception as exc:  # noqa: BLE001 — 软门不因检测失败断流
        return [{
            "kind": "contradiction", "severity": "info",
            "message": f"矛盾检测通道失败：{exc}", "suggestion": "人工复核简报一致性",
        }]
    if not answer or answer.upper().startswith("无矛盾") or answer == "[]":
        return []
    return [{
        "kind": "contradiction", "severity": "high",
        "message": f"简报内部矛盾：{answer[:500]}",
        "suggestion": "修订矛盾处后再提交（或知情带病提交）",
    }]


def preflight(md: str, *,
              checker: Callable[[str], str] | None = None) -> dict[str, Any]:
    """软门预检主入口：问题清单＋修改建议；不强制断流。"""
    problems = structure_check(md) + vagueness_check(md) + contradiction_check(md, checker)
    return {
        "problems": problems,
        "counts": {
            "high": sum(1 for p in problems if p["severity"] == "high"),
            "medium": sum(1 for p in problems if p["severity"] == "medium"),
            "info": sum(1 for p in problems if p["severity"] == "info"),
        },
        "verdict": "clean" if not problems else "needs-review",
    }
