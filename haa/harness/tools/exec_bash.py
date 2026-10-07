"""工具①：执行命令＋后台任务（exec_bash / job_output / job_kill）。

大修第一章 §5.1 规格的 M1 实现，行为规格来自 DSH 测试翻译清单 §1：
- 前台：输出＋末尾强制 `[exit code: N]`；超时进程组击杀（超时标记在前、
  信号标记在后）；trap 信号后 exit 0 仍带超时标记；输出截断 32KB
  头 25%＋尾 75%；无输出渲染 `(no output)`；stderr 段以 `[stderr]` 标记；
  二进制乱码不崩（errors=replace＋说明行）。
- 后台：JobManager 发放 job_id；job_output 消费式增量读＋状态行；
  job_kill 进程组终止（TERM→宽限→KILL）；wait 帽。
- 环境变量白名单制传递；单次超时被配置上限封顶。
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.jobs import JobManager, _spawn_group

logger = logging.getLogger("haa.harness.tools.bash")

DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024
DEFAULT_GRACE_MS = 500
DEFAULT_ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")


def truncate_output(text: str, cap: int = DEFAULT_MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """32KB 截断纪律：头 25%＋尾 75%，中间省略标注（第一章 §5.1）。"""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= cap:
        return text, False
    head = max(1, int(cap * 0.25))
    tail = max(1, cap - head)
    omitted = len(data) - head - tail
    out = (
        data[:head].decode("utf-8", errors="replace")
        + f"\n……（省略 {omitted} 字符）……\n"
        + data[-tail:].decode("utf-8", errors="replace")
    )
    return out, True


def _decode_with_note(data: bytes) -> tuple[str, bool]:
    """UTF-8 解码；含不可解码字节时 replace 并标记（二进制乱码不崩）。"""
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def render_output(
    stdout: str,
    stderr: str,
    *,
    exit_code: int | None,
    timed_out: bool = False,
    signal_name: str | None = None,
    timeout_ms: int | None = None,
    binary_replaced: bool = False,
    truncated: bool = False,
) -> str:
    """结果渲染（DSH renderResult 语义的 HAA 翻译）。

    段序：stdout →（分隔）[stderr] 段 → 标记行（超时 → 信号 → 退出码）；
    全空渲染 ``(no output)``。标记行只在输出未以换行结尾时先补换行。
    """
    parts: list[str] = []
    if stdout:
        parts.append(stdout if stdout.endswith("\n") or not stderr else stdout + "\n")
    if stderr:
        parts.append("[stderr]\n" + stderr)
    body = "\n".join(parts)
    if binary_replaced:
        body = "[binary output replaced non-UTF-8 bytes]\n" + body if body \
            else "[binary output replaced non-UTF-8 bytes]"
    if truncated:
        body = (body + "\n") if body and not body.endswith("\n") else body
        body += "[output truncated — re-run with narrower scope if you need the middle]"
    markers: list[str] = []
    if timed_out:
        markers.append(f"[timed out after {timeout_ms}ms]")
    if signal_name:
        markers.append(f"[killed by signal: {signal_name}]")
    if exit_code is not None and not timed_out:
        markers.append(f"[exit code: {exit_code}]")
    if not body.strip():
        body = "(no output)"  # 正文为空（标记行仍照加，§1-2）
    text = body
    for m in markers:
        text = (text + "\n") if text and not text.endswith("\n") else text
        text += m
    return text


class ForegroundRunner:
    """前台命令执行（阻塞、超时进程组击杀）。"""

    def __init__(self, *, max_timeout_ms: int = 600_000,
                 grace_ms: int = DEFAULT_GRACE_MS,
                 max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES):
        self.max_timeout_ms = max_timeout_ms
        self.grace_ms = grace_ms
        self.max_output_bytes = max_output_bytes

    def clamp_timeout(self, timeout_ms: Any) -> int:
        try:
            t = int(timeout_ms)
        except (TypeError, ValueError):
            t = DEFAULT_TIMEOUT_MS
        return max(1, min(t, self.max_timeout_ms))

    def run(self, command: str, *, cwd: str | None = None,
            env: dict | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> ToolResult:
        t0 = time.monotonic()
        effective = self.clamp_timeout(timeout_ms)
        proc = _spawn_group(command, cwd=cwd, env=env)
        timed_out = False
        signal_name: str | None = None
        try:
            out_b, err_b = proc.communicate(timeout=effective / 1000.0)
        except Exception:  # noqa: BLE001 — TimeoutExpired：进程组击杀后收尸
            timed_out = True
            self._terminate_group(proc)
            try:
                out_b, err_b = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out_b, err_b = b"", b""
        rc = proc.poll()
        if rc is not None and rc < 0:
            # 超时击杀也报信号标记（DSH：超时标记在前、信号标记在后）
            signal_name = f"SIG{signal.Signals(-rc).name}"
        stdout, bin1 = _decode_with_note(out_b or b"")
        stderr, bin2 = _decode_with_note(err_b or b"")
        rendered = render_output(
            stdout, stderr,
            exit_code=rc if rc is not None else None,
            timed_out=timed_out, signal_name=signal_name,
            timeout_ms=effective, binary_replaced=bin1 or bin2,
        )
        text, truncated = truncate_output(rendered, self.max_output_bytes)
        return ToolResult(content=text, meta={
            "exit_code": rc, "timed_out": timed_out, "truncated": truncated,
            "duration_s": time.monotonic() - t0,
        })

    def _terminate_group(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=self.grace_ms / 1000.0)
        except Exception:  # noqa: BLE001 — 宽限未退，升级 KILL
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def filtered_env(extra: dict | None = None,
                 whitelist: tuple[str, ...] = DEFAULT_ENV_WHITELIST) -> dict:
    """环境变量白名单制传递：白名单外的宿主变量不透传。"""
    env = {k: v for k, v in os.environ.items()
           if k in whitelist or any(k.startswith(p) for p in ("LC_",))}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def status_line(snapshot) -> str:
    """job_output 的终态状态行。"""
    bits = [f"status: {snapshot.state}"]
    if snapshot.exit_code is not None:
        bits.append(f"exit code: {snapshot.exit_code}")
    if snapshot.signal:
        bits.append(f"signal: {snapshot.signal}")
    if snapshot.timed_out:
        bits.append("timed out")
    return "[" + ", ".join(bits) + "]"


def make_handlers(services) -> dict[str, Any]:
    """构建三个工具的 handler（闭包持有 services：JobManager/配置）。"""

    def exec_bash(args: dict, ctx: ToolCallContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("exec_bash: 'command' must be a non-empty string")
        cwd = args.get("cwd") or None
        if cwd is not None and not services.path_allowed(str(cwd)):
            raise ToolError(f"exec_bash: cwd outside sandbox roots: {cwd}")
        env = filtered_env()
        if args.get("run_in_background"):
            job_id = services.jobs.start(
                command, label=command[:80], owner=ctx.campaign_id or "",
                cwd=cwd, env=env,
            )
            return ToolResult(
                content=f"started background job {job_id} — use job_output to read it, "
                        f"job_kill to stop it",
                meta={"job_id": job_id},
            )
        # legacy 契约兼容：timeout（秒）优先，其次 timeout_ms（毫秒）
        timeout_ms = DEFAULT_TIMEOUT_MS
        if isinstance(args.get("timeout"), (int, float)) and args["timeout"] > 0:
            timeout_ms = int(args["timeout"]) * 1000
        elif isinstance(args.get("timeout_ms"), (int, float)) and args["timeout_ms"] > 0:
            timeout_ms = int(args["timeout_ms"])
        return services.foreground.run(command, cwd=cwd, env=env, timeout_ms=timeout_ms)

    def job_output(args: dict, ctx: ToolCallContext) -> ToolResult:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            raise ToolError("job_output: 'job_id' must be non-empty")
        offset = args.get("offset")
        offset = int(offset) if isinstance(offset, (int, float)) else None
        wait = bool(args.get("wait"))
        if wait:
            cap_s = services.max_wait_s
            try:
                want = float(args.get("wait_timeout_s", cap_s))
            except (TypeError, ValueError):
                want = cap_s
            services.jobs.get(job_id).wait(timeout_s=min(want, cap_s))
        try:
            text, snap, nxt, lossy = services.jobs.read(job_id, offset)
        except ToolError:
            raise
        parts = [text] if text else []
        if lossy:
            parts.append("[earlier output was discarded to bound memory — offsets before "
                         f"{nxt - len(text.encode())} are unavailable]")
        if not snap.state == "running":
            parts.append(status_line(snap))
        else:
            parts.append("[status: running — call job_output again for more]")
        body = "\n".join(p for p in parts if p)
        body, _ = truncate_output(body)
        return ToolResult(content=body, meta={"next_offset": nxt, "state": snap.state})

    def job_kill(args: dict, ctx: ToolCallContext) -> ToolResult:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            raise ToolError("job_kill: 'job_id' must be non-empty")
        reason = str(args.get("reason") or "job_kill")
        msg = services.jobs.kill(job_id, reason=reason)
        return ToolResult(content=msg)

    return {"exec_bash": exec_bash, "job_output": job_output, "job_kill": job_kill}


EXIT_CODE_HABIT = (
    "Every command result ends with a status marker line such as "
    "'[exit code: 0]'. ALWAYS check it before assuming success — a non-zero "
    "code or '[timed out after …]' means the command failed; read the output "
    "above (including any [stderr] section) before retrying differently."
)
