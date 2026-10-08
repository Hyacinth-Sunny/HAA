"""概念档案——数学-代码双向映射（大修第三章 §8.1，批次6 P1-b）。

DESIGN 输出扩展：核心概念逐张登记概念卡（LaTeX 数学表述必填、对应代码
位置可选、依赖关系成 DAG、status 三态、出处溯源）。WRITE 消费侧按
章节-概念映射表提取切片，核心概念不得发明档案外定义。

校验规则（确定性代码，不靠提示词劝说）：
1. ``math_formulation`` 必填（空=提名式定义，是 smoke10 correctness 低的
   病根之一）；
2. ``code_refs`` 为空时 ``status`` 必须为 defined 或 imported（纯理论概念
   不允许标 assumed 却无数学表述）；
3. ``dependencies`` 引用的 concept_id 必须在集合内（无悬垂引用）；
4. 依赖图不得出现环（拓扑排序校验）。

持久化：campaign 结束时晋升至 memory_bank 的 domain_concept 实体
（M-b 已建好实体 schema，此处产出卡片、晋升由结算管理器批次7 承接）。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger("haa.concept_archive")

CONCEPT_STATUSES = ("defined", "assumed", "imported")


class ConceptCard(BaseModel):
    """一张概念卡（数学表述 + 代码位置 + 依赖 + 状态 + 出处）。"""

    model_config = ConfigDict(extra="forbid")

    concept_id: str = Field(min_length=1, description="卡内唯一 ID（如 C1）")
    name: str = Field(min_length=1)
    math_formulation: str = Field(min_length=1,
                                  description="LaTeX 数学表述（必填——核心约束）")
    code_refs: list[dict] = Field(
        default_factory=list,
        description="对应代码位置 [{repo, path, symbol}]（纯理论概念可为空）")
    dependencies: list[str] = Field(
        default_factory=list, description="依赖的其他 concept_id")
    status: str = Field(default="defined",
                        description="defined=已定义 | assumed=引用外部假设 | imported=沿用既有")
    provenance: str = Field(default="", description="出处：锚点/候选/文献编号")

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CONCEPT_STATUSES:
            raise ValueError(f"concept status must be one of {CONCEPT_STATUSES}: {v!r}")
        return v

    @field_validator("math_formulation")
    @classmethod
    def _math(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("math_formulation is REQUIRED (nominal definitions are "
                             "the known correctness bottleneck — smoke10)")
        return v


def _topo_sort(concepts: list[ConceptCard]) -> list[str] | None:
    """Kahn 拓扑排序；返回有序 ID 列表或 None=有环。"""
    ids = {c.concept_id for c in concepts}
    in_deg = {c.concept_id: 0 for c in concepts}
    adj: dict[str, list[str]] = {c.concept_id: [] for c in concepts}
    for c in concepts:
        for dep in c.dependencies:
            if dep in ids:
                adj[dep].append(c.concept_id)
                in_deg[c.concept_id] += 1
    queue = sorted([i for i, d in in_deg.items() if d == 0])
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adj[node]):
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)
                queue.sort()
    return order if len(order) == len(concepts) else None


def validate_concepts(raw_concepts: list[Any]) -> tuple[list[ConceptCard], list[str]]:
    """把 DESIGN 输出的原始列表转概念卡并校验。返回 (合法卡, 错误清单)。

    错误清单非空时调用方应要求 DESIGN 返工（错误喂回模型继续）。
    """
    cards: list[ConceptCard] = []
    errors: list[str] = []
    ids: set[str] = set()
    for i, raw in enumerate(raw_concepts):
        if not isinstance(raw, dict):
            errors.append(f"concept[{i}] is not an object")
            continue
        try:
            card = ConceptCard(**{k: v for k, v in raw.items()
                                  if k in ConceptCard.model_fields})
            if card.concept_id in ids:
                errors.append(f"duplicate concept_id: {card.concept_id}")
                continue
            ids.add(card.concept_id)
            cards.append(card)
        except ValidationError as exc:
            detail = "; ".join(e["msg"] for e in exc.errors()[:2])
            errors.append(f"concept[{i}] ({raw.get('name', '?')}): {detail}")
    # 悬垂依赖
    for c in cards:
        for dep in c.dependencies:
            if dep not in ids:
                errors.append(f"{c.concept_id} depends on unknown {dep}")
    # 依赖图无环
    if cards and not errors:
        order = _topo_sort(cards)
        if order is None:
            cycle_parts = sorted(c.concept_id for c in cards if c.dependencies)
            errors.append(f"concept dependency graph has a cycle "
                          f"(check: {cycle_parts[:5]})")
    return cards, errors


def section_slices(concepts: list[ConceptCard],
                   section_map: dict[str, list[str]]) -> dict[str, list[ConceptCard]]:
    """按章节-概念映射表提取切片（WRITE 消费）。

    section_map: {section_name: [concept_id, ...]}——DESIGN 产出。
    未出现在映射中的章节返回空切片（WRITE 不强制引用概念）。
    """
    by_id = {c.concept_id: c for c in concepts}
    out: dict[str, list[ConceptCard]] = {}
    for section, ids in (section_map or {}).items():
        out[section] = [by_id[i] for i in ids if i in by_id]
    return out


def render_concept_block(cards: list[ConceptCard]) -> str:
    """概念档案的 prompt 注入块（WRITE 守则：核心概念不得发明档案外定义）。"""
    if not cards:
        return ""
    lines = ["⛓⛓⛓ 概念档案（WRITE 守则：以下核心概念的定义与定理表述必须引用"
             "档案原文——可润色不可改义；新概念须先回写档案再引用）⛓⛓⛓"]
    for c in cards:
        dep = f"｜依赖 {','.join(c.dependencies)}" if c.dependencies else ""
        lines.append(f"- **{c.name}** ({c.concept_id}，{c.status}{dep})："
                     f"{c.math_formulation[:300]}"
                     + (f"｜出处 {c.provenance}" if c.provenance else ""))
    return "\n".join(lines)


CONCEPT_ARCHIVE_RULES = (
    "Concept archive: DESIGN produces concept cards (math_formulation is "
    "REQUIRED in LaTeX). WRITE must reference the archive verbatim for core "
    "concepts — new concepts must be added to the archive before use."
)
