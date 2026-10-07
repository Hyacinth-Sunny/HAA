"""防脑裂三件套 v1（大修第三章 §4.3）。

1. **偏差说明（anchor_diff）**：锚定模式下每个环节的输出结构增加
   ``{consistent: [要点], deviations: [走样点+原因]}``；deviations 为空时
   必须显式写"无偏差"。本模块提供解析与校验。
2. **保真检查器（v1 关键词版）**：计划书允许 v1 用关键词与引用核对替代
   模型评分——对锚点三要素（主张/机制/判据）在环节产出中的术语覆盖做
   五点评分（主张/机制/判据/边界/术语各 0-2 分），总分低于 7/10 触发
   报警标记（进入 context.extra 供 Web 呈现与日志）。
3. **异议上报契约**：任何环节认为锚点有原理性/实践性缺陷时，唯一合法
   动作是产出 ``{challenge_type, evidence, suggested_action}`` 异议报告，
   管线暂停等用户裁决（禁止悄悄改写锚点）。

脑裂（术语）＝用户给定的观点在流程传递中被模型悄悄改写。
"""

from __future__ import annotations

import re
from typing import Any

CHALLENGE_TYPES = ("原理不可行", "实践不可行", "先例已存在", "范围建议")
FIDELITY_ALARM_THRESHOLD = 7  # /10


def parse_anchor_diff(data: dict[str, Any]) -> dict | None:
    """解析环节输出中的 anchor_diff 字段（缺省 None；结构坏返回带错标记）。"""
    raw = data.get("anchor_diff")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return {"consistent": [], "deviations": ["anchor_diff 结构非法：非对象"]}
    consistent = [str(x) for x in (raw.get("consistent") or [])]
    deviations = [str(x) for x in (raw.get("deviations") or [])]
    if not deviations and not raw.get("deviations_explicit_none"):
        # 计划书：deviations 为空时必须显式写"无偏差"
        ctext = "".join(consistent)
        if "无偏差" not in ctext and not deviations:
            deviations = ["（未显式声明无偏差）"]
    return {"consistent": consistent, "deviations": deviations}


def parse_challenge(data: dict[str, Any]) -> dict | None:
    """解析异议报告（challenge 或 challenge_report 键）。"""
    raw = data.get("challenge") or data.get("challenge_report")
    if not isinstance(raw, dict):
        return None
    ctype = str(raw.get("challenge_type", "")).strip()
    evidence = str(raw.get("evidence", "")).strip()
    action = str(raw.get("suggested_action", "")).strip()
    if not (ctype and evidence):
        return None
    return {"challenge_type": ctype, "evidence": evidence,
            "suggested_action": action}


def _key_terms(text: str, limit: int = 12) -> list[str]:
    """从一段中文/英文混合文本抽关键术语（≥2 字的词块，去停用词）。"""
    stop = {"的", "了", "在", "是", "和", "与", "或", "一个", "我们", "可以",
            "通过", "对于", "以及", "这个", "that", "the", "with", "for",
            "and", "via", "using", "based"}
    words = [w for w in re.split(r"[^\w一-鿿]+", text or "")
             if len(w) >= 2 and w.lower() not in stop]
    # 保序去重，长词优先
    seen, out = set(), []
    for w in sorted(words, key=len, reverse=True)[: limit * 2]:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def fidelity_score(anchor, stage_output: str) -> dict:
    """五点保真评分（各 0-2 分，总 /10）。

    v1 关键词版（计划书明示可用；成本低、可后升级为模型评分）：
    - 主张覆盖：锚点 core_claim 关键术语在产出中的命中率
    - 机制覆盖：expected_mechanism 术语命中
    - 判据覆盖：success_criteria 术语命中
    - 边界说明：产出含边界类措辞（边界/适用范围/假设/限制）
    - 术语一致：锚点核心术语原样出现（非改写）
    """
    text = stage_output or ""

    def coverage(source: str) -> int:
        terms = _key_terms(source, 8)
        if not terms:
            return 1
        hits = sum(1 for t in terms if t in text)
        ratio = hits / len(terms)
        return 2 if ratio >= 0.6 else (1 if ratio >= 0.25 else 0)

    claim_terms = _key_terms(anchor.core_claim, 5)
    verbatim = sum(1 for t in claim_terms if t in text)
    terminology = 2 if (not claim_terms or verbatim >= max(1, len(claim_terms) - 1)) \
        else (1 if verbatim else 0)
    boundary = 2 if re.search(r"边界|适用范围|假设|限制|boundary|scope|assumption",
                              text) else 0
    points = {
        "claim": coverage(anchor.core_claim),
        "mechanism": coverage(anchor.expected_mechanism),
        "criteria": coverage(anchor.success_criteria),
        "boundary": boundary,
        "terminology": terminology,
    }
    total = sum(points.values())
    return {"points": points, "total": total, "max": 10,
            "alarm": total < FIDELITY_ALARM_THRESHOLD}


def check_stage_output(anchor, result_data: dict[str, Any]) -> dict:
    """对一个环节产出做三件套检查；返回 {anchor_diff, challenge, fidelity}。

    供 pipeline 在锚定模式下逐环节调用；challenge 非空 → 管线暂停。
    """
    out: dict[str, Any] = {
        "anchor_diff": parse_anchor_diff(result_data),
        "challenge": parse_challenge(result_data),
        "fidelity": None,
    }
    searchable = " ".join(
        str(v) for v in result_data.values() if isinstance(v, (str, int, float))
    )
    out["fidelity"] = fidelity_score(anchor, searchable)
    return out


ANCHOR_GUARD_CLAUSE = """
### ⚓ 锚定模式纪律（最高优先级）
你在锚定模式下工作：简报的 hypothesis_anchor 是**待检验的对象，不是被复制的目标**。
- 每个输出必须包含顶层字段 "anchor_diff": {"consistent": [...], "deviations": [...]}
  （deviations 为空时 consistent 里必须显式写"无偏差"）。
- 若你认为锚点有原理性或实践性缺陷，**禁止修改它**；你的唯一合法动作是在输出
  顶层加 "challenge": {"challenge_type": "原理不可行|实践不可行|先例已存在|范围建议",
  "evidence": "...", "suggested_action": "..."}——管线会暂停交用户裁决。
"""
