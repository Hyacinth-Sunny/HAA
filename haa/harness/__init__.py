"""HAA 自建 Harness（大修计划书第一章）——工具注册、提示词楼层、统一检查层、
会话事件日志与工具调用循环。

M0 状态：核心五件套就位；16 个旧工具经 :mod:`haa.harness.tools_bridge`
换壳接入（行为由旧 handler 保证）；``haa/llm/tools.py`` 旧注册表在迁移
完成前保留。各阶段（stage）的提示词投稿与工具逐个迁入随后续批次进行。
"""

from haa.harness.registry import (
    ALL_STAGES,
    ToolCallContext,
    ToolMenu,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolError,
    tool,
)
from haa.harness.prompt_sections import (
    PromptSectionRegistry,
    Section,
)

__all__ = [
    "ALL_STAGES",
    "ToolCallContext",
    "ToolMenu",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolError",
    "tool",
    "PromptSectionRegistry",
    "Section",
]
