"""会话事件日志——「先记账再干活」铁律的落地（大修计划书第一章 §3.5）。

事件类型首批（新命名，斜杠分层）::

    tool/call    工具调用前记账（阶段、工具名、参数、时间戳）
    tool/result  工具结果冻结记账（执行后才允许追加，此后任何处理不得改写）
    llm/call     模型调用（token/耗时）——由 client 侧事件平移补充
    llm/result   模型返回
    job/state    后台任务/实验状态变化（M2 起用）

**向后兼容（M0 关键约束）**：现有 ``haa/observability.py::tool_call_stats``
聚合的是旧事件名 ``tool_call``。本模块在发新事件的同时**镜像发一条旧名
``tool_call``**（同一 payload 结构），保证 /api/tool-stats 与 analyze_campaign
在大修期间不中断；旧名事件在旧注册表删除时一并退役。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("haa.harness.session_log")

# 新事件类型常量（唯一登记处；向后兼容的旧名镜像见 _LEGACY_MIRROR）
EVENT_TOOL_CALL = "tool/call"
EVENT_TOOL_RESULT = "tool/result"
EVENT_LLM_CALL = "llm/call"
EVENT_LLM_RESULT = "llm/result"
EVENT_JOB_STATE = "job/state"

_LEGACY_MIRROR = {EVENT_TOOL_CALL: "tool_call"}


class SessionEventLog:
    """包一层事件 sink：只做命名/冻结纪律，不自己建存储。

    - ``event_sink`` 沿用 ``haa.observability`` 的 EventSink 体系
      （SQLiteEventSink / NullEventSink），落盘进现有 events 表——
      向后兼容旧日志，不新建通道。
    - **结果冻结**：``tool_result`` 对同一 (stage, tool, seq) 只发一次；
      重复调用直接丢弃并打日志（§3.4 第 6 步铁律）。
    """

    def __init__(self, event_sink: Any = None):
        from haa.observability import NullEventSink

        self._sink = event_sink if event_sink is not None else NullEventSink()
        self._seq = 0
        self._frozen: set[tuple[str, str, int]] = set()

    # -- 基础发射 -----------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        *,
        campaign_id: str | None = None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        duration_s: float | None = None,
    ) -> None:
        try:
            self._sink.emit(
                event_type=event_type,
                campaign_id=campaign_id,
                stage=stage,
                duration_s=duration_s,
                payload=payload or {},
            )
        except Exception:  # noqa: BLE001 — 记账失败不炸工具执行（sink 自身也永不抛）
            logger.exception("session event emit failed: %s", event_type)

    # -- 工具两跳（先记账再干活 / 结果冻结） ---------------------------------

    def tool_call(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        stage: str = "",
        campaign_id: str = "",
    ) -> int:
        """第 1 步：调用前记账。返回本次调用的序号（供 result 配对）。"""
        self._seq += 1
        payload = {
            "tool": tool,
            "arguments": arguments,
            "arguments_bytes": len(str(arguments)),
            "call_seq": self._seq,
        }
        cid = campaign_id or None
        st = stage or None
        self._emit(EVENT_TOOL_CALL, campaign_id=cid, stage=st, payload=payload)
        if EVENT_TOOL_CALL in _LEGACY_MIRROR:
            mirror = dict(payload)
            mirror.pop("call_seq", None)
            self._emit(_LEGACY_MIRROR[EVENT_TOOL_CALL], campaign_id=cid, stage=st, payload=mirror)
        return self._seq

    def tool_result(
        self,
        *,
        tool: str,
        call_seq: int,
        ok: bool,
        error: str | None = None,
        result_head: str = "",
        duration_s: float = 0.0,
        meta: dict[str, Any] | None = None,
        stage: str = "",
        campaign_id: str = "",
    ) -> bool:
        """第 6 步：结果记账（冻结）。同一 call_seq 只允许一次。"""
        key = (stage, tool, call_seq)
        if key in self._frozen:
            logger.warning("tool/result re-emit blocked (frozen): %s", key)
            return False
        self._frozen.add(key)
        payload = {
            "tool": tool,
            "ok": ok,
            "error": error,
            "result_head": result_head,
            "call_seq": call_seq,
            **(meta or {}),
        }
        self._emit(
            EVENT_TOOL_RESULT,
            campaign_id=campaign_id or None,
            stage=stage or None,
            payload=payload,
            duration_s=duration_s,
        )
        # 旧名镜像：observability.tool_call_stats 依赖（ok/error/result_head/
        # arguments_bytes 字段结构保持旧口径）。
        self._emit(
            "tool_call",
            campaign_id=campaign_id or None,
            stage=stage or None,
            duration_s=duration_s,
            payload={
                "tool": tool,
                "ok": ok,
                "error": error,
                "arguments_bytes": payload.get("arguments_bytes", 0),
                "result_head": result_head[:120],
            },
        )
        return True

    # -- 其他事件类型（占位登记，M1/M2 起接入） ------------------------------

    def job_state(self, *, job_id: str, state: str, detail: str = "", campaign_id: str = "") -> None:
        self._emit(
            EVENT_JOB_STATE,
            campaign_id=campaign_id or None,
            payload={"job_id": job_id, "state": state, "detail": detail},
        )


class _RecordingSink:
    """测试/回放用的内存 sink（不落盘）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, *, event_type: str, campaign_id=None, stage=None,
             cost_usd=None, tokens=None, duration_s=None, payload=None) -> None:
        self.events.append({
            "event_type": event_type,
            "campaign_id": campaign_id,
            "stage": stage,
            "duration_s": duration_s,
            "payload": dict(payload or {}),
            "ts": time.time(),
        })
