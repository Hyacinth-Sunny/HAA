"""工具注册表——大修计划书第一章 §3.2「四件套」格式的落地。

四件套 = ①名字（全库唯一、蛇形）②用途说明（给模型看）③参数 JSON Schema
（给模型看＋代码校验）④阶段菜单（本工具在哪些 stage 可见）。

与旧 ``haa/llm/tools.py`` 硬编码列表的关系（第一章 §9 兼容条款）：
- 旧注册表在 M0 迁移完成前保留；M0 期间新 Harness 经
  :mod:`haa.harness.tools_bridge` 把旧 16 工具"换壳"进本注册表——
  行为仍由旧 handler 保证，注册格式与执行通道（统一检查层+事件日志）
  换成新的。
- 后续批次逐工具迁入 ``haa/harness/tools/``，迁完后删除旧路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("haa.harness.registry")

ALL_STAGES = "*"


class ToolError(Exception):
    """工具执行失败（结果会作为错误文本喂回模型，不静默死亡）。"""


@dataclass
class ToolResult:
    """工具的结构化结果（对齐 DSH defineTool 的 output 语义）。

    M0 换壳期 ``content`` 即旧 handler 的字符串返回；``meta`` 携带
    统一检查层的计量信息（耗时、截断标记等），不进模型可见内容。
    """

    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallContext:
    """一次工具调用的上下文（新 Harness 侧）。

    换壳桥接期真正的执行上下文由旧 registry 自行构造；这里只承载
    新检查层需要的信息。
    """

    stage_name: str = ""
    campaign_id: str = ""


# handler 签名：(args: dict, ctx: ToolCallContext) -> ToolResult | str
ToolHandler = Callable[[dict, ToolCallContext], Any]


@dataclass
class ToolSpec:
    """四件套的代码形态。"""

    name: str
    description: str
    parameters: dict[str, Any]
    stages: tuple[str, ...] | str  # ALL_STAGES 或 stage 名元组
    handler: ToolHandler
    guardrails: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # 登记来源（哪个模块/批次迁入），供审计

    def is_allowed_for(self, stage_name: str) -> bool:
        if self.stages == ALL_STAGES or "*" in (
            self.stages if isinstance(self.stages, tuple) else ()
        ):
            return True
        if not stage_name:
            return True  # 无阶段上下文 → 不拦（测试/临时调用，旧注册表同语义）
        return stage_name in self.stages

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    stages: tuple[str, ...] | str,
    guardrails: dict[str, Any] | None = None,
    source: str = "",
):
    """四件套装饰器：把一个函数登记为 Harness 工具。

    用法（新工具的标准写法，第一批迁移目标见第一章 §4）::

        @tool(
            name="glob",
            description="按文件名模式找文件。",
            parameters={"type": "object", "properties": {...}, "required": [...]},
            stages=("SEEK", "NOVELTY", ...),
            guardrails={"reads_paths": True},
        )
        def glob_(args: dict, ctx: ToolCallContext) -> ToolResult:
            ...

    装饰后函数获得 ``.haa_tool_spec: ToolSpec`` 属性；调用
    :meth:`ToolRegistry.register_spec` 完成入表（注册表实例由调用方
    持有，模块级全局注册表不存在——每个 AgentLoop 一张表，带各自的
    检查层状态）。
    """

    def decorator(fn: ToolHandler) -> ToolHandler:
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            stages=stages,
            handler=fn,
            guardrails=dict(guardrails or {}),
            source=source or getattr(fn, "__module__", ""),
        )
        fn.haa_tool_spec = spec  # type: ignore[attr-defined]
        return fn

    return decorator


class ToolMenu:
    """声明式阶段工具菜单（第一章 §3.7；配置文件 ``config/tool_menu.yaml``）。

    文件格式为「工具 → 阶段列表」（与代码内四件套的 stages 声明同构，
    可直接 diff）。**按工具覆盖**语义：文件中登记的工具，其 stages 以
    文件为准；未登记的工具沿用代码内声明（因此全阶段 ``*`` 工具不必
    进文件——代码声明原样生效）。文件缺失时完全回退代码。

    M0 的文件内容从旧注册表 ``allowed_stages`` 逐字生成（行为等价快照）；
    后续调菜单只改文件不改代码。
    """

    def __init__(self, tool_to_stages: dict[str, list[str]] | None = None):
        self._tool_to_stages: dict[str, list[str]] = {
            str(k): [str(s).upper() for s in (v or [])] for k, v in (tool_to_stages or {}).items()
        }

    @classmethod
    def load(cls, path: str | Path) -> "ToolMenu":
        p = Path(path)
        if not p.is_absolute():
            from haa.config import _PROJECT_ROOT

            p = _PROJECT_ROOT / p
        if not p.exists():
            logger.warning("tool_menu file not found (%s) — falling back to code-declared stages", p)
            return cls(None)
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(data if isinstance(data, dict) else None)

    def stage_set(self, tool_name: str) -> set[str] | None:
        """None = 该工具无菜单覆盖（用代码声明）；set = 覆盖后的阶段集。"""
        stages = self._tool_to_stages.get(tool_name)
        return set(stages) if stages is not None else None

    def tools(self) -> list[str]:
        return sorted(self._tool_to_stages)

    def tool_to_stages(self) -> dict[str, list[str]]:
        return dict(self._tool_to_stages)


class ToolRegistry:
    """新 Harness 的工具注册表 + 执行入口（执行走统一检查层）。

    与旧 ``haa.llm.tools.ToolRegistry`` 的同名方法是**鸭子类型兼容**的：
    ``get_schemas(stage_name)`` / ``execute(name, arguments, *, stage_name,
    campaign_id)``——但 ``execute`` 返回 :class:`ToolResult`（含 meta），
    且每次调用必经 :mod:`haa.harness.checklist` 六步链。
    """

    def __init__(self, *, checklist: Any | None = None, menu: ToolMenu | None = None):
        self._tools: dict[str, ToolSpec] = {}
        self.checklist = checklist  # None 时在首次 execute 前必须注入（见 _ensure_checklist）
        self.menu = menu or ToolMenu(None)

    # -- 注册 ---------------------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec

    def register_spec(self, fn: ToolHandler) -> None:
        spec = getattr(fn, "haa_tool_spec", None)
        if spec is None:
            raise ValueError("function has no .haa_tool_spec — decorate it with @tool()")
        self.register(spec)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- 查询 ---------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def is_allowed_for(self, name: str, stage_name: str) -> bool:
        spec = self._tools.get(name)
        if spec is None:
            return False
        menu_stages = self.menu.stage_set(name)
        if menu_stages is not None:
            return stage_name in menu_stages
        return spec.is_allowed_for(stage_name)

    def get_schemas(self, stage_name: str) -> list[dict[str, Any]]:
        """按 stage 过滤出 OpenAI 工具 schema 列表（菜单覆盖优先）。"""
        return [
            spec.to_openai_schema()
            for name, spec in sorted(self._tools.items())
            if self.is_allowed_for(name, stage_name)
        ]

    # -- 执行 ---------------------------------------------------------------

    def _ensure_checklist(self):
        if self.checklist is None:
            # 延迟导入避免环：checklist 依赖 session_log/registry 两边。
            from haa.harness.checklist import Checklist

            self.checklist = Checklist()
        return self.checklist

    def attach_session_log(self, session_log: Any) -> None:
        """把调用方的会话日志接到检查层（幂等）。

        AgentLoop 构造时持有 event_sink；注册表若由桥接预先装配则已带同一
        日志，直连注册表（测试/复用）时由此补接——保证 tool/call、
        tool/result 两跳与循环侧同本账。
        """
        checklist = self._ensure_checklist()
        if checklist.session_log is not session_log:
            checklist.session_log = session_log

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        stage_name: str = "",
        campaign_id: str = "",
    ) -> ToolResult:
        """执行一个工具（必经统一检查层六步链）。"""
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"unknown tool: {name}")
        if not self.is_allowed_for(name, stage_name):
            raise ToolError(
                f"tool {name!r} not allowed in stage {stage_name!r} (menu/declaration)"
            )
        return self._ensure_checklist().run(
            spec,
            arguments,
            ToolCallContext(stage_name=stage_name, campaign_id=campaign_id),
        )

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        stage_name: str = "",
        campaign_id: str = "",
    ) -> ToolResult:
        """兼容别名：与旧注册表同名同签名（返回值升级为 ToolResult）。"""
        return self.invoke(
            name, arguments, stage_name=stage_name, campaign_id=campaign_id
        )
