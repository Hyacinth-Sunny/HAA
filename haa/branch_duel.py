"""P1-c：top-k 分支树 + GRADE 分型对决（大修批次9，第三章 §9/§10）。

分支树：候选队列从线性列表改为树结构——VERIFY/PILOT 杀死当前分支→标记
dead+死因→取次优 alive 分支继续；全部 dead→沿用队列耗尽语义。

GRADE 分型对决：候选标注 theory/experimental/systems/benchmark 四型，
同型内两两对决（瑞士制+随机换序），胜场排名替代绝对分。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("haa.branch_duel")

PAPER_TYPES = ("theory", "experimental", "systems", "benchmark")
DEFAULT_BRANCH_BUDGET_FACTOR = 0.6
DEFAULT_MAX_BRANCHES = 3


# --------------------------------------------------------------------------- #
#  top-k 分支树（第三章 §9）
# --------------------------------------------------------------------------- #

@dataclass
class BranchNode:
    """分支树节点（候选+存活状态+子分支）。"""

    candidate_id: str
    slug: str
    title: str
    grade_score: float = 0.0
    status: str = "alive"  # alive | dead | regressed
    kill_reason: str = ""
    kill_stage: str = ""
    children: list["BranchNode"] = field(default_factory=list)
    parent_id: str | None = None
    branch_budget_used: float = 0.0

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "slug": self.slug,
            "title": self.title,
            "grade_score": self.grade_score,
            "status": self.status,
            "kill_reason": self.kill_reason,
            "kill_stage": self.kill_stage,
            "children": [c.to_dict() for c in self.children],
            "parent_id": self.parent_id,
        }


class BranchTree:
    """top-k 分支树管理（§9：回退语义+预算门控）。"""

    def __init__(self, *, max_branches: int = DEFAULT_MAX_BRANCHES,
                 budget_factor: float = DEFAULT_BRANCH_BUDGET_FACTOR):
        self.max_branches = max_branches
        self.budget_factor = budget_factor
        self.roots: list[BranchNode] = []
        self._total_branches = 0

    def add_root(self, candidate) -> BranchNode:
        node = BranchNode(
            candidate_id=candidate.id,
            slug=candidate.slug,
            title=candidate.title,
        )
        self.roots.append(node)
        self._total_branches += 1
        return node

    def kill_branch(self, slug: str, *, stage: str, reason: str) -> None:
        """标记 dead + 死因——不删树（供 EA 门分支树视图）。"""
        for node in self._all_nodes():
            if node.slug == slug and node.status == "alive":
                node.status = "dead"
                node.kill_reason = reason
                node.kill_stage = stage
                break

    def next_alive(self) -> BranchNode | None:
        """取分数最高的 alive 分支（top-k 回退语义）。"""
        alive = [n for n in self._all_nodes() if n.status == "alive"]
        if not alive:
            return None
        return max(alive, key=lambda n: n.grade_score)

    def alive_count(self) -> int:
        return sum(1 for n in self._all_nodes() if n.status == "alive")

    def can_spawn_branch(self, *, base_budget: float) -> bool:
        """预算门控：分支数未超帽 且 剩余预算充足。"""
        if self._total_branches >= self.max_branches:
            return False
        cost = base_budget * self.budget_factor
        total_used = sum(n.branch_budget_used for n in self._all_nodes())
        return cost <= (1.0 - min(total_used, 1.0)) if base_budget <= 1.0 else True

    def _all_nodes(self) -> list[BranchNode]:
        out: list[BranchNode] = []
        stack = list(self.roots)
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(node.children)
        return out

    def render_tree_view(self) -> str:
        """EA 门呈现义务：分支树视图（死活+死因）。"""
        lines: list[str] = []
        for root in self.roots:
            self._render_node(root, 0, lines)
        return "\n".join(lines) if lines else "(empty branch tree)"

    def _render_node(self, node: BranchNode, depth: int, lines: list) -> None:
        icon = {"alive": "✓", "dead": "✗", "regressed": "↩"}.get(node.status, "?")
        lines.append(f"{'  ' * depth}{icon} {node.title} ({node.slug})")
        if node.status == "dead" and node.kill_reason:
            lines.append(f"{'  ' * depth}  死因：[{node.kill_stage}] {node.kill_reason[:120]}")
        for child in node.children:
            self._render_node(child, depth + 1, lines)


# --------------------------------------------------------------------------- #
#  GRADE 分型对决（第三章 §10）
# --------------------------------------------------------------------------- #

def classify_paper_type(title: str, rationale: str = "",
                        positive_claim: str = "") -> str:
    """轻量分型判定（v1 确定性关键词；v2 换 LLM 判定）。"""
    text = f"{title} {rationale} {positive_claim}".lower()
    if any(w in text for w in ("proof", "theorem", "bound", "impossibility",
                                 "lower bound", "upper bound", "证明", "定理",
                                 "下界", "上界", "不可能")):
        return "theory"
    if any(w in text for w in ("benchmark", "dataset", "evaluation",
                                 "基准", "评测")):
        return "benchmark"
    if any(w in text for w in ("system", "architecture", "implementation",
                                 "protocol", "部署", "架构", "实现")):
        return "systems"
    return "experimental"


@dataclass
class DuelResult:
    """一次两两对决的结果。"""

    winner: str  # slug
    loser: str
    reason: str


def swiss_pairings(candidates: list[dict], *, rng: random.Random | None = None,
                   paper_types: dict[str, str] | None = None) -> list[tuple[dict, dict]]:
    """瑞士制配对：同型优先、近分优先、随机换序。"""
    rng = rng or random.Random()
    types = paper_types or {}
    typed = [(c, types.get(c.get("slug", ""), classify_paper_type(
        c.get("title", ""), c.get("rationale", ""), c.get("positive_claim", ""))))
        for c in candidates]
    # 按类型分组
    by_type: dict[str, list] = {}
    for c, t in typed:
        by_type.setdefault(t, []).append(c)
    pairs: list[tuple[dict, dict]] = []
    for t, group in sorted(by_type.items()):
        rng.shuffle(group)  # 随机换序（防位置偏差）
        # 近分配对（瑞士制：胜场相近的对决）
        for i in range(0, len(group) - 1, 2):
            pairs.append((group[i], group[i + 1]))
    return pairs


def render_duel_prompt(cand_a: dict, cand_b: dict, paper_type: str) -> str:
    """对决判定 prompt（甲乙随机排序，输出"甲优/乙优/相当"+一句理由）。"""
    import random
    first, second = (cand_a, cand_b) if random.random() < 0.5 else (cand_b, cand_a)
    return f"""You are a research paper quality judge. Compare two candidates of the
same paper type ({paper_type}). Randomly ordered:

## Paper 甲
Title: {first.get('title', '?')}
Positive claim: {first.get('positive_claim', '')}
Rationale: {first.get('rationale', '')[:500]}

## Paper 乙
Title: {second.get('title', '?')}
Positive claim: {second.get('positive_claim', '')}
Rationale: {second.get('rationale', '')[:500]}

## Judging criteria (same type, so directly comparable):
- Technical soundness: is the core mechanism well-grounded?
- Significance: does it address a real gap?
- Feasibility: can the claims be validated?

Output a single JSON: {{"winner": "甲" | "乙" | "相当", "reason": "one sentence"}}
"""


def parse_duel_verdict(text: str, slug_a: str, slug_b: str,
                       first_is_a: bool) -> DuelResult | None:
    """解析对决 JSON，映射回 slug。"""
    import json as _json
    try:
        data = _json.loads(text.strip())
    except (ValueError, TypeError):
        return None
    winner_label = str(data.get("winner", "")).strip()
    reason = str(data.get("reason", "")).strip()
    if winner_label == "甲":
        winner = slug_a if first_is_a else slug_b
        loser = slug_b if first_is_a else slug_a
    elif winner_label == "乙":
        winner = slug_b if first_is_a else slug_a
        loser = slug_a if first_is_a else slug_b
    else:
        return None  # 相当 or unparseable
    return DuelResult(winner=winner, loser=loser, reason=reason)


def compute_duel_rankings(candidates: list[dict],
                          duel_results: list[DuelResult]) -> list[dict]:
    """胜场排名（写入工件供 ARV 人工再审）。"""
    wins: dict[str, int] = {c.get("slug", ""): 0 for c in candidates}
    losses: dict[str, int] = {c.get("slug", ""): 0 for c in candidates}
    for d in duel_results:
        wins[d.winner] = wins.get(d.winner, 0) + 1
        losses[d.loser] = losses.get(d.loser, 0) + 1
    ranked = sorted(candidates, key=lambda c: (-wins.get(c.get("slug", ""), 0),
                                               losses.get(c.get("slug", ""), 0)))
    return [{"slug": c.get("slug", ""), "title": c.get("title", ""),
             "wins": wins.get(c.get("slug", ""), 0),
             "losses": losses.get(c.get("slug", ""), 0)}
            for c in ranked]
