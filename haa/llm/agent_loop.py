"""ReAct agent loop — "model calls a tool → code runs it → result goes back".

This is the engine a stage uses when it wants tool access. It does NOT decide
the pipeline's next stage (that stays in haa/pipeline.py); it just runs one
bounded tool-using conversation inside a single stage. Per HAA's philosophy,
the loop is *code-driven*: every call goes through :meth:`LLMClient.call_with_tools`
(so budget/retry/timeout all apply — HM-Pro Lessons 5 & 7), and the
:class:`~haa.llm.tools.ToolRegistry` filters which tools the current ``stage_name``
may even see.

Loop::

    messages = [system?, user]
    loop:
        resp = client.call_with_tools(messages, stage-filtered tools)
        append assistant message
        if resp has no tool_calls → done (resp.content is the final answer)
        else: run every tool_call, append each result, count calls
              if calls >= max_tool_calls → one final no-tools call, stop (truncated)

Guarantees
----------
* **Bounded**: ``max_tool_calls`` caps the total tool executions (default 10).
* **Message integrity**: every assistant ``tool_calls`` message is followed by a
  ``tool`` response for each ``tool_call_id`` (required by the OpenAI format), so
  the cap is checked *after* a batch completes.
* **No silent death**: a tool that raises is caught and its error string is fed
  back to the model as the tool result (the loop continues).
* **Context survives** (HM-Pro Lesson 1): the full transcript is returned on
  :attr:`AgentLoopResult.messages` so a stage can persist it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from haa.llm.client import LLMClient
from haa.llm.tools import ToolError, ToolRegistry

logger = logging.getLogger("haa.agent")


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
    """The outcome of one :meth:`AgentLoop.run`."""

    content: str  # the model's final (non-tool) answer
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    iterations: int = 0  # number of LLM completions
    truncated: bool = False  # hit max_tool_calls
    total_cost_usd: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)  # full transcript


class AgentLoop:
    """Drive a single tool-using conversation for one stage."""

    def __init__(
        self,
        client: LLMClient,
        tools: ToolRegistry | None,
        *,
        logger: logging.Logger | None = None,
        tool_result_max_chars: int = 8000,
        read_file_result_max_chars: int = 30000,
        event_sink: Any = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self._log = logger or globals()["logger"]
        # 循环内上下文压缩（smoke4：工具结果全文进 messages 无界累积）。
        self.tool_result_max_chars = int(tool_result_max_chars)
        self.read_file_result_max_chars = int(read_file_result_max_chars)
        # 工具级用量事件（v1.0.6-rev2）：每个 tool_call 落 events 表——
        # stage_tool_limits 调参从"人工翻日志"变数据驱动。默认 Null（测试）。
        from haa.observability import NullEventSink
        self.event_sink = event_sink if event_sink is not None else NullEventSink()

    def _conv_cap(self, tool_name: str) -> int:
        """Per-tool char cap for tool results entering the live conversation.

        read_file 独立大帽（22KB 级知识文件必须整读）；v1.0.6-rev3 起
        read_pdf / fetch_paper_fulltext 同级 30K——论文全文被 8K 通用帽
        截断等于白取（审判端读全文是刚需）。
        """
        if tool_name in ("read_file", "read_pdf", "fetch_paper_fulltext"):
            return self.read_file_result_max_chars
        return self.tool_result_max_chars

    def _conv_tool_content(self, tool_name: str, content: str) -> str:
        """Tool result bounded for the live conversation (full text still goes
        to the history/transcript) — 模型能看到截断标记与被截字符数。"""
        cap = self._conv_cap(tool_name)
        text = content or ""
        if len(text) <= cap:
            return text
        return text[:cap] + f"\n…[{len(text) - cap} chars truncated; ask again with narrower scope if you need the tail]"

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

        ``json_mode`` requests a JSON final answer by appending an instruction to
        the system prompt (tools and strict ``response_format`` don't mix across
        providers, so we don't combine them — strict JSON stages use
        :meth:`LLMClient.call_json` directly).

        ``deadline_s`` is the whole-stage wall-clock budget (``timeouts.stage``，
        v1.0.2 复活——此前是死配置). 工具帽与死线同款收尾：到点后不再执行
        工具，一次无工具调用索要最终答案（照常出判决，``truncated=True``）。
        """
        sys_msg = system_prompt or ""
        if json_mode:
            sys_msg = (sys_msg + "\n\n" if sys_msg else "") + (
                "Respond ONLY with a single valid JSON object."
            )

        messages: list[dict[str, Any]] = []
        if sys_msg:
            messages.append({"role": "system", "content": sys_msg})
        messages.append({"role": "user", "content": prompt})

        # Offer tools only if the stage has any AND a budget for them.
        schemas = self.tools.get_schemas(stage_name) if self.tools else []
        offer_tools: list[dict[str, Any]] | None = schemas if (schemas and max_tool_calls > 0) else None

        history: list[ToolInvocation] = []
        iterations = 0
        truncated = False
        cost = 0.0
        calls_made = 0
        last_content = ""
        loop_start = time.monotonic()  # stage 死线的锚点

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

            # Execute every tool call in this assistant turn (keeps the
            # tool_call_id ↔ tool-response pairing the API requires intact).
            for tc in tool_calls:
                calls_made += 1
                fn = tc.get("function") or {}
                name = str(fn.get("name", ""))
                args = self._parse_args(fn.get("arguments", "{}"))
                start = time.monotonic()
                try:
                    if self.tools is None:
                        raise ToolError("no tool registry configured")
                    result = self.tools.execute(
                        name, args, stage_name=stage_name, campaign_id=campaign_id
                    )
                    err = None
                except Exception as exc:  # feed the error back, don't crash the loop
                    result = ""
                    err = f"{type(exc).__name__}: {exc}"
                duration = time.monotonic() - start
                self._log.info(
                    "tool %s ok=%s %.2fs stage=%s", name, err is None, duration, stage_name
                )
                self.event_sink.emit(
                    event_type="tool_call",
                    campaign_id=campaign_id or None,
                    stage=stage_name or None,
                    duration_s=duration,
                    payload={
                        "tool": name,
                        "ok": err is None,
                        "error": err,
                        "arguments_bytes": len(str(args)),
                        # 结果头 120 字符：fetch_paper_fulltext 等工具的来源/
                        # 字符数头（[fulltext | source=… chars=…]）据此可查。
                        "result_head": (result or "")[:120],
                    },
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

            # Cap reached / stage deadline hit → finalize with one no-tools call
            # so we still get an answer (same graceful exit either way).
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
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"({reason} — give your final answer now "
                            "using the information gathered)"
                        ),
                    }
                )
                iterations += 1
                final = self.client.call_with_tools(
                    messages, None, campaign_id=campaign_id or None, stage=stage_name or None
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

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _assistant_message(resp: Any) -> dict[str, Any]:
        """Rebuild the assistant turn to append to the transcript."""
        msg: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
        calls = AgentLoop._normalize_tool_calls(resp.tool_calls)
        if calls:
            msg["tool_calls"] = calls
        return msg

    @staticmethod
    def _normalize_tool_calls(calls: Any) -> list[dict[str, Any]]:
        """Coerce litellm tool_call objects (or dicts) into plain dicts.

        litellm returns ``ChatCompletionMessageToolCall`` pydantic objects at
        runtime; unit tests pass plain dicts. Normalise so downstream ``.get()``
        works AND the assistant message we re-send is plain JSON — strict
        providers (e.g. DeepSeek) reject a ``tool_calls`` assistant turn whose
        ``tool_call_id``s have no matching ``tool`` response, which silently
        breaks if ids are dropped during object→dict coercion.
        """
        out: list[dict[str, Any]] = []
        for tc in calls or []:
            if isinstance(tc, dict):
                out.append(tc)
            elif hasattr(tc, "model_dump"):
                out.append(tc.model_dump())
            else:  # fallback attribute access
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
