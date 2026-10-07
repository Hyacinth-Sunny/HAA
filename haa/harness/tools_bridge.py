"""M0 换壳桥接——把旧 ``haa/llm/tools.py`` 的 16 个工具接入新 Harness 注册表。

换壳语义（大修计划书第一章 §4.1/§9 兼容条款）：
- **注册格式换成四件套**（名字/说明/参数 schema/阶段菜单原样照搬）；
- **执行通道换成新检查层**（先记账→路径白名单→执行→写闸门→结果处理→
  冻结记账），行为本体仍由旧 handler 保证（沙箱/黑名单/SSRF 闸等安全
  设施在旧 handler 内原样生效，新检查层的白名单是同纪律的第二道闸）；
- 后续批次逐工具迁入 ``haa/harness/tools/``，全部迁完后删除旧路径与
  本桥接。

guardrails 推断（旧定义无此字段，按下表补声明，供检查层路由）：
- reads_paths: grep / read_file / read_pdf / fetch_paper_fulltext（路径型参数）
- writes_paths: write_file / edit_file
- runs_commands: exec_bash
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from haa.harness.registry import (
    ToolCallContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger("haa.harness.bridge")

_READ_PATH_TOOLS = {"grep", "read_file", "read_pdf", "fetch_paper_fulltext"}
_WRITE_PATH_TOOLS = {"write_file", "edit_file"}
_COMMAND_TOOLS = {"exec_bash"}


def _guardrails_for(name: str) -> dict[str, Any]:
    g: dict[str, Any] = {}
    if name in _READ_PATH_TOOLS:
        g["reads_paths"] = True
    if name in _WRITE_PATH_TOOLS:
        g["writes_paths"] = True
    if name in _COMMAND_TOOLS:
        g["runs_commands"] = True
    return g


def registry_from_legacy(
    legacy: Any,
    *,
    event_sink: Any = None,
    write_gate_enabled: bool = False,
    menu: Any = None,
) -> ToolRegistry:
    """把一个旧 :class:`haa.llm.tools.ToolRegistry` 换壳成新注册表。

    ``legacy`` 的安全配置（campaigns_dir / tools config / client / vision）
    逐工具透传给旧 handler（构造旧 ToolCallContext），行为零变化。
    """
    from haa.harness.checklist import (
        Checklist,
        PathWhitelist,
        ReadBeforeWriteGate,
    )
    from haa.harness.registry import ToolMenu
    from haa.harness.session_log import SessionEventLog

    legacy_ctx_type = _legacy_ctx_type()

    def make_handler(legacy_tool: Any):
        def handler(args: dict, ctx: ToolCallContext) -> ToolResult:
            legacy_ctx = legacy_ctx_type(
                stage_name=ctx.stage_name,
                campaign_id=ctx.campaign_id,
                campaigns_dir=legacy.campaigns_dir,
                allowed_roots=legacy.allowed_roots,
                config=legacy.config,
                client=legacy.client,
                vision_config=legacy.vision_config,
            )
            out = legacy_tool.handler(args if isinstance(args, dict) else {}, legacy_ctx)
            return ToolResult(content="" if out is None else str(out))

        return handler

    registry = ToolRegistry(
        checklist=Checklist(
            session_log=SessionEventLog(event_sink),
            prechecks=[PathWhitelist(allowed_roots=(Path(legacy.campaigns_dir),))],
            write_gate=ReadBeforeWriteGate(enabled=write_gate_enabled),
        ),
        menu=menu if menu is not None else ToolMenu(None),
    )
    for name in legacy.names():
        legacy_tool = legacy.get(name)
        stages_typed = tuple(sorted(legacy_tool.allowed_stages))
        if "*" in stages_typed:
            stages_typed = "*"  # type: ignore[assignment]
        spec = ToolSpec(
            name=legacy_tool.name,
            description=legacy_tool.description,
            parameters=legacy_tool.parameters,
            stages=stages_typed,
            handler=make_handler(legacy_tool),
            guardrails=_guardrails_for(name),
            source="legacy-bridge",
        )
        registry.register(spec)
    logger.debug("harness bridge: %d tools re-shelled", len(registry.names()))
    return registry


def _legacy_ctx_type() -> Any:
    from haa.llm.tools import ToolCallContext as LegacyCtx

    return LegacyCtx
