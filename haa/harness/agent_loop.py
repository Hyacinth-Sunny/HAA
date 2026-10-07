"""新 Harness 的工具调用循环——自 ``haa/llm/agent_loop.py`` 迁移改造（第一章 §3.6）。

哲学全部保留（原模块 docstring 的保证逐条继承）：
- **代码驱动**：模型不知道自己在第几阶段；每次调用过 ``LLMClient.call_with_tools``
  （预算先扣/重试/超时全在 client 侧）；
- **stage 菜单过滤**：``registry.get_schemas(stage_name)`` 决定模型能看见哪些工具
  （菜单文件覆盖优先，§3.7）；
- **有界**：``max_tool_calls`` 帽 + stage 墙钟死线，两路同款优雅收尾
  （一次无工具调用索要最终答案，``truncated=True``）；
- **消息配对完整**：assistant ``tool_calls`` 后必跟每个 id 的 ``tool`` 应答；
- **不静默死亡**：工具错误文本作为结果喂回模型继续。

升级点（相对旧循环）：
- 工具执行走 **统一检查层**（六步链：先记账→检查→执行→写闸门→结果处理→
  冻结记账），事件（tool/call、tool/result＋旧名镜像）由
  :class:`~haa.harness.session_log.SessionEventLog` 在检查层内发射——
  本循环不再直接 emit tool_call（避免双份账）；
- 系统提示词可选经**楼层注册表**组装（``system_prompt`` 为 None 且
  ``sections`` 已投稿时）；显式传入 ``system_prompt`` 时原样使用
  （M0 兼容期各 stage 仍传 Jinja2 渲染串）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from haa.harness.registry import ToolRegistry
from haa.harness.session_log import SessionEventLog

logger = logging.getLogger("haa.harness.agent")


def _truncate(text: str, limit: int = 500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


@dataclass
class ToolInvocation:
    """One executed tool call (history entry)."""

    name: str
    arguments: dict[str, Any]
    result: str
    error: str | None
    duration_s: float


@dataclass
class AgentLoopResult:
    """The outcome of one :meth:`AgentLoop.run` (与旧循环字段逐一兼容)."""

    content: str
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    iterations: int = 0
    truncated: bool = False
    total_cost_usd: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """Drive a single tool-using conversation for one stage (new harness)."""

    def __init__(
        self,
        client: Any,
        tools: ToolRegistry | None,
        *,
        logger: logging.Logger | None = None,
        tool_result_max_chars: int = 8000,
        read_file_result_max_chars: int = 30000,
        event_sink: Any = None,
        sections: Any = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self._log = logger or globals()["logger"]
        # 会话内上下文压缩：工具结果进对话的 per-tool 字符帽（M0 沿用旧值；
        # M1 结果处理模块统一 32KB 纪律后，此帽退化为二道保险）。
        self.tool_result_max_chars = int(tool_result_max_chars)
        self.read_file_result_max_chars = int(read_file_result_max_chars)
        self.session_log = SessionEventLog(event_sink)
        self.sections = sections  # PromptSectionRegistry | None（可选楼层组装）

    # -- 会话内字符帽（与旧循环同款语义） ------------------------------------

    def _conv_cap(self, tool_name: str) -> int:
        if tool_name in ("read_file", "read_pdf", "fetch_paper_fulltext"):
            return self.read_file_result_max_chars
        return self.tool_result_max_chars

    def _conv_tool_content(self, tool_name: str, content: str) -> str:
        cap = self._conv_cap(tool_name)
        text = content or ""
        if len(text) <= cap:
            return text
        return text[:cap] + (
            "\n…[" + str(len(text) - cap) +
            " chars truncated; ask again with narrower scope if you need the tail]"
        )

    # -- 主循环 ---------------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        stage_name: str = "",
        campaign_id: str = "",
        max_tool_calls: int = 10,
        json_mode: bool = False,
        deadline_s: float | None = None,
    ) -> AgentLoopResult:
        """Run the ReAct loop and return the final answer + transcript.

        签名与返回结构同旧 ``haa.llm.agent_loop.AgentLoop.run``——stages 与
        测试无需改动即可切换执行引擎。
        """
        if self.tools is not None:
            # 工具事件与循环同本账（桥接已接时为幂等空操作）
            self.tools.attach_session_log(self.session_log)
        sys_msg = system_prompt
        if sys_msg is None and self.sections is not None:
            sys_msg = self.sections.assemble({"stage": stage_name})
        sys_msg = sys_msg or ""
        if json_mode:
            sys_msg = (sys_msg + "\n\n" if sys_msg else "") + (
                "Respond ONLY with a single valid JSON object."
            )

        messages: list[dict[str, Any]] = []
        if sys_msg:
            messages.append({"role": "system", "content": sys_msg})
        messages.append({"role": "user", "content": prompt})

        schemas = self.tools.get_schemas(stage_name) if self.tools else []
        offer_tools: list[dict[str, Any]] | None = (
            schemas if (schemas and max_tool_calls > 0) else None
        )

        history: list[ToolInvocation] = []
        iterations = 0
        truncated = False
        cost = 0.0
        calls_made = 0
        last_content = ""
        loop_start = time.monotonic()

        while True:
            iterations += 1
            resp = self.client.call_with_tools(
                messages,
                offer_tools,
                campaign_id=campaign_id or None,
                stage=stage_name or None,
            )
            cost += getattr(resp.usage, "cost_usd", 0.0) or 0.0
            messages.append(self._assistant_message(resp))

            tool_calls = self._normalize_tool_calls(resp.tool_calls)
            if not tool_calls:
                last_content = resp.content
                break

            for tc in tool_calls:
                calls_made += 1
                fn = tc.get("function") or {}
                name = str(fn.get("name", ""))
                args = self._parse_args(fn.get("arguments", "{}"))
                start = time.monotonic()
                try:
                    if self.tools is None:
                        from haa.harness.registry import ToolError

                        raise ToolError("no tool registry configured")
                    result_obj = self.tools.invoke(
                        name, args, stage_name=stage_name, campaign_id=campaign_id
                    )
                    result = getattr(result_obj, "content", result_obj) or ""
                    err = None
                except Exception as exc:  # feed the error back, don't crash the loop
                    result = ""
                    err = type(exc).__name__ + ": " + str(exc)
                duration = time.monotonic() - start
                self._log.info(
                    "tool %s ok=%s %.2fs stage=%s", name, err is None, duration, stage_name
                )
                history.append(
                    ToolInvocation(
                        name=name,
                        arguments=args,
                        result=_truncate(result),
                        error=err,
                        duration_s=duration,
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "")),
                        "content": self._conv_tool_content(name, err or result),
                    }
                )

            deadline_hit = (
                deadline_s is not None
                and calls_made > 0
                and time.monotonic() > (loop_start + deadline_s)
            )
            if calls_made >= max_tool_calls or deadline_hit:
                truncated = True
                reason = (
                    "tool-call budget reached"
                    if calls_made >= max_tool_calls
                    else "stage wall-clock deadline reached"
                )
                messages.append({
                    "role": "user",
                    "content": "(" + reason +
                               " — give your final answer now using the information gathered)",
                })
                iterations += 1
                final = self.client.call_with_tools(
                    messages, None,
                    campaign_id=campaign_id or None, stage=stage_name or None,
                )
                cost += getattr(final.usage, "cost_usd", 0.0) or 0.0
                messages.append(self._assistant_message(final))
                last_content = final.content
                break

        return AgentLoopResult(
            content=last_content,
            tool_calls=history,
            iterations=iterations,
            truncated=truncated,
            total_cost_usd=cost,
            messages=messages,
        )

    # -- helpers（自旧循环原样迁移） ------------------------------------------

    @staticmethod
    def _assistant_message(resp: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
        calls = AgentLoop._normalize_tool_calls(resp.tool_calls)
        if calls:
            msg["tool_calls"] = calls
        return msg

    @staticmethod
    def _normalize_tool_calls(calls: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tc in calls or []:
            if isinstance(tc, dict):
                out.append(tc)
            elif hasattr(tc, "model_dump"):
                out.append(tc.model_dump())
            else:
                fn = getattr(tc, "function", None)
                out.append({
                    "id": getattr(tc, "id", "") or "",
                    "type": getattr(tc, "type", "function") or "function",
                    "function": {
                        "name": getattr(fn, "name", "") if fn else "",
                        "arguments": getattr(fn, "arguments", "{}") if fn else "{}",
                    },
                })
        return out

    @staticmethod
    def _parse_args(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
