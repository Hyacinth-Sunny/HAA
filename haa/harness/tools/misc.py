"""M2 新增工具起步件：glob（按文件名模式找文件）与 budget_status（预算只读查询）。

对照第一章 §4.2 第一批：glob 全阶段；budget_status 面向 P2（菜单随后续
P2 批次铺设，注册即可用）。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.exec_bash import truncate_output

GLOB_TOOL_RULES = (
    "File discovery: use glob with a filename pattern (e.g. '*.md', 'exp_*.py') "
    "to locate files before reading; pair with grep for content search."
)

BUDGET_TOOL_RULES = (
    "Budget: budget_status reports remaining USD and token caps for the "
    "current campaign (read-only). Check it before launching large work."
)


def make_handlers(services) -> dict[str, Any]:

    def glob_(args: dict, ctx: ToolCallContext) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern or any(c in pattern for c in ("\x00",)):
            raise ToolError("glob: 'pattern' must be a non-empty filename pattern")
        max_results = 200
        try:
            max_results = max(1, min(int(args.get("max_results", 200)), 500))
        except (TypeError, ValueError):
            pass
        matches: list[str] = []
        for root in services.allowed_roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            for p in root_path.rglob("*"):
                if fnmatch.fnmatch(p.name, pattern):
                    matches.append(str(p))
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if not matches:
            return ToolResult(content=f"(no files matching {pattern!r})")
        body, _ = truncate_output("\n".join(matches), 8000)
        return ToolResult(content=body, meta={"count": len(matches)})

    def budget_status(args: dict, ctx: ToolCallContext) -> ToolResult:
        budget = services.budget_ref() if services.budget_ref else None
        if budget is None or not ctx.campaign_id:
            return ToolResult(
                content="budget status unavailable (no budget manager or campaign "
                        "context) — proceed with normal frugality")
        store = budget.store
        camp = store.get_campaign(ctx.campaign_id)
        if camp is None:
            raise ToolError(f"budget_status: unknown campaign {ctx.campaign_id}")
        cfg = getattr(budget, "config", None)
        tok_used = store.sum_tokens(ctx.campaign_id)
        tok_cap = getattr(cfg, "per_campaign_tokens", 0) or 0
        lines = [
            f"campaign USD: used ${camp.budget_used:.2f} / limit ${camp.budget_limit:.2f} "
            f"(remaining ${max(camp.budget_remaining, 0):.2f})",
        ]
        if tok_cap:
            lines.append(f"campaign tokens: used {tok_used:,} / cap {tok_cap:,} "
                         f"({max(tok_cap - tok_used, 0):,} remaining)")
        else:
            lines.append(f"campaign tokens: used {tok_used:,} (no token cap set)")
        glob_tok = getattr(cfg, "global_tokens", 0) or 0
        if glob_tok:
            lines.append(f"global tokens: used {store.sum_tokens(None):,} / cap "
                         f"{glob_tok:,}")
        return ToolResult(content="\n".join(lines))

    return {"glob": glob_, "budget_status": budget_status}
