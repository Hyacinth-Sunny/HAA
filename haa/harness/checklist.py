"""统一检查层——所有工具调用必经的六步链（大修计划书第一章 §3.4）。

顺序固定，不可跳步::

    1. 记账        tool/call（先记账再干活）
    2. 事前检查链  路径白名单 → 命令黑名单 → 沙箱策略 →（未来）审批
                   结论只有放行/拒绝；前面的检查拒绝了后面不能翻案
    3. 执行体      调 handler；耗时计量；异常捕获为 ToolError
    4. 写闸门      仅写/改类工具——先读后写 + mtime 未变（特性开关，
                   M1 随五个底层工具强化后默认开）
    5. 结果处理    截断规则、退出码标记、渲染提示
                   （M0 换壳期=透传：截断/标记纪律仍由旧 handler 与
                   agent 循环的会话内字符帽承担；M1 换成 32KB 头25%尾75%）
    6. 记账        tool/result（从此冻结）

每个环节是独立小模块（实现 ``PreCheck`` / ``WriteGate`` / ``ResultProcessor``
协议），从 ``config/*.yaml`` 读配置——策略与工具解耦：白名单模块不需要
认识 bash 工具，超时模块不需要认识文件工具。

性能预算（第一章 §6.4）：单次调用的检查层＋记账开销 ≤ 50ms 量级
（不含工具执行本体）——本链全程内存操作，唯一 IO 是事件 sink 落库
（SQLiteEventSink 自身即 insert 一行）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from haa.harness.registry import (
    ToolCallContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from haa.harness.session_log import SessionEventLog

logger = logging.getLogger("haa.harness.checklist")


# --- 协议：可插拔小模块 -------------------------------------------------------


class PreCheck(Protocol):
    """事前检查（第 2 步链上的一环）。拒绝时抛 ToolError。"""

    def check(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> None: ...


class WriteGate(Protocol):
    """写闸门（第 4 步）。拒绝时抛 ToolError。"""

    def enforce(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> None: ...


class ResultProcessor(Protocol):
    """结果处理（第 5 步）。返回（可能加工过的）ToolResult。"""

    def process(self, result: ToolResult, spec: ToolSpec) -> ToolResult: ...


# --- 具体模块 -----------------------------------------------------------------


@dataclass
class PathWhitelist:
    """路径白名单：声明 ``reads_paths``/``writes_paths`` 守则的工具，
    其 path 类参数（符号链接解析后）必须落在某个允许根内。

    M0 换壳期与旧 ``_safe_resolve`` 同一条纪律（campaigns 目录沙箱），
    双重检查无害；M1 沙箱模块（第一章 §5.3）就位后成为唯一实现。
    """

    allowed_roots: tuple[Path, ...] = ()
    path_arg_names: tuple[str, ...] = ("path", "file", "target")

    def check(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> None:
        touches = spec.guardrails.get("reads_paths") or spec.guardrails.get("writes_paths")
        if not touches or not self.allowed_roots:
            return
        raw = None
        for key in self.path_arg_names:
            v = args.get(key)
            if isinstance(v, str) and v:
                raw = v
                break
        if not raw:
            return
        p = Path(raw).expanduser()
        # 相对路径按第一个允许根解析（与旧 read_file 的 campaigns 沙箱语义一致）
        candidates = [p] if p.is_absolute() else [Path(root) / p for root in self.allowed_roots]
        resolved = candidates[0].resolve()
        for cand in candidates:
            for root in self.allowed_roots:
                try:
                    cand.resolve().relative_to(Path(root).resolve())
                    return  # 在白名单内
                except ValueError:
                    continue
        raise ToolError(
            "path outside allowed roots: " + str(raw) +
            " (resolved=" + str(resolved) + ") — sandbox violation"
        )


@dataclass
class ReadTracker:
    """先读后写状态（写闸门的数据源）：记录本阶段循环内读过的文件与 mtime。

    状态生命周期 = 一个 AgentLoop 实例（= 一次 stage 会话），换工具/
    换候选即换循环——与"本阶段循环内已读过"的语义一致。
    """

    reads: dict[str, float] = field(default_factory=dict)  # resolved path → mtime

    def record_read(self, path: str) -> None:
        try:
            rp = str(Path(path).expanduser().resolve())
            self.reads[rp] = os.path.getmtime(rp)
        except OSError:
            self.reads[str(path)] = -1.0

    def may_write(self, path: str) -> tuple[bool, str]:
        rp = str(Path(path).expanduser().resolve())
        if rp not in self.reads:
            return False, f"write gate: {path} was never read in this stage loop"
        mtime = self.reads[rp]
        if mtime <= 0:
            return True, ""
        try:
            if os.path.getmtime(rp) != mtime:
                return False, f"write gate: {path} changed on disk since last read (mtime)"
        except OSError:
            return True, ""  # 文件已被删等场景交给 handler 报错
        return True, ""


@dataclass
class ReadBeforeWriteGate:
    """第 4 步写闸门（第一章 §5.2 的强制机制，代码强制非提示词劝说）。

    M0 默认关（``harness.write_gate.enabled: false``——Ch6 特性开关纪律：
    出问题关开关即回退）；M1 随 edit_file/write_file 重写后默认开。
    """

    enabled: bool = False
    tracker: ReadTracker = field(default_factory=ReadTracker)
    write_arg_names: tuple[str, ...] = ("path", "file", "target")

    def enforce(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> None:
        # 读类工具先登记（供后续写校验）——读本身不受闸门约束
        if spec.guardrails.get("reads_paths") is True:
            raw = self._first_path(args)
            if raw:
                self.tracker.record_read(raw)
        if spec.guardrails.get("writes_paths") is not True:
            return
        if not self.enabled:
            return
        raw = self._first_path(args)
        if not raw:
            return
        ok, why = self.tracker.may_write(raw)
        if not ok:
            raise ToolError(why)

    def _first_path(self, args: dict[str, Any]) -> str | None:
        for key in self.write_arg_names:
            v = args.get(key)
            if isinstance(v, str) and v:
                return v
        return None


@dataclass
class PassthroughProcessor:
    """第 5 步 M0 形态：透传 + 计量入 meta。M1 换 TruncatingProcessor。"""

    def process(self, result: ToolResult, spec: ToolSpec) -> ToolResult:
        result.meta.setdefault("chars", len(result.content or ""))
        return result


# --- 六步链 -------------------------------------------------------------------


class Checklist:
    """六步链编排器。每个 ToolRegistry 持有一个实例（携带闸门状态）。"""

    def __init__(
        self,
        *,
        session_log: SessionEventLog | None = None,
        prechecks: list[PreCheck] | None = None,
        write_gate: ReadBeforeWriteGate | None = None,
        processor: ResultProcessor | None = None,
    ):
        self.session_log = session_log or SessionEventLog()
        self.prechecks: list[PreCheck] = prechecks or []
        self.write_gate = write_gate or ReadBeforeWriteGate()
        self.processor = processor or PassthroughProcessor()
        self._chain_overhead_budget_s = 0.050  # §6.4 性能预算（测试锚定）

    def run(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        t0 = time.monotonic()
        # 1. 记账（先记账再干活）
        call_seq = self.session_log.tool_call(
            tool=spec.name, arguments=args, stage=ctx.stage_name, campaign_id=ctx.campaign_id
        )
        # 2. 事前检查链：拒绝即第 6 步记失败账并抛出（结论不可翻案）
        try:
            for check in self.prechecks:
                check.check(spec, args, ctx)
        except ToolError as exc:
            self._freeze(
                spec, ctx, call_seq, ok=False, error=str(exc), result="", t0=t0
            )
            raise
        # 3. 执行体
        err: str | None = None
        result = ToolResult()
        try:
            out = spec.handler(args, ctx)
            if isinstance(out, ToolResult):
                result = out
            else:
                result = ToolResult(content="" if out is None else str(out))
        except Exception as exc:  # noqa: BLE001 — 不静默死亡：错误文本喂回模型
            err = f"{type(exc).__name__}: {exc}"
        # 4. 写闸门（仅写/改类工具；M0 默认关）
        if err is None:
            try:
                self.write_gate.enforce(spec, args, ctx)
            except ToolError as exc:
                err = str(exc)
                result = ToolResult()
        # 5. 结果处理
        if err is None:
            result = self.processor.process(result, spec)
        # 6. 记账（冻结）
        self._freeze(
            spec,
            ctx,
            call_seq,
            ok=err is None,
            error=err,
            result=result.content if err is None else "",
            t0=t0,
            extra_meta=dict(result.meta) if err is None else {},
        )
        if err is not None:
            raise ToolError(err)
        return result

    def _freeze(
        self,
        spec: ToolSpec,
        ctx: ToolCallContext,
        call_seq: int,
        *,
        ok: bool,
        error: str | None,
        result: str,
        t0: float,
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        duration = time.monotonic() - t0
        self.session_log.tool_result(
            tool=spec.name,
            call_seq=call_seq,
            ok=ok,
            error=error,
            result_head=(result or "")[:120],
            duration_s=duration,
            meta=extra_meta,
            stage=ctx.stage_name,
            campaign_id=ctx.campaign_id,
        )
