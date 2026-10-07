"""后台任务基座——五态统一状态模型与 JobManager（大修第一章 §5.1/§5.5）。

状态模型（本地后台任务与远端实验共用同一套状态词——第一章 §5.5）::

    running → completed | failed | killed | timeout     （终态不回退）

- completed：进程退出（任意退出码——非零退出码是数据不是错误，标记在结果里）
- failed   ：内部错误（启动失败/收集器崩溃）
- killed   ：被信号杀死（含 job_kill 与进程自杀）
- timeout  ：超时击杀（kill 的带因变体，供监控区分死因）

每次状态迁移经 :func:`validate_transition` 合法性校验并对外发射
``job/state`` 事件（经检查层的会话日志，未来仪表板直接订阅）。

移植对照（DSH 测试翻译清单 §5）：快照一致性矩阵、合法前驱、id 前缀计数、
每 owner 并发帽、消费式读取、kill 幂等与 already-finished、wait 帽。

安全形态：子进程一律 ``["/bin/bash", "-lc", command]`` 固定三元素参数
列表 + ``start_new_session=True`` 独立进程组（超时/击杀整组回收），
无 shell 字符串拼接；命令黑名单在统一检查层事前拦截。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from subprocess import PIPE, Popen
from typing import Callable

from haa.harness.registry import ToolError

logger = logging.getLogger("haa.harness.jobs")

# 终态集与合法迁移表（状态只能从合法前驱进入）
TERMINAL_STATES = frozenset({"completed", "failed", "killed", "timeout"})
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset(TERMINAL_STATES),
    **{t: frozenset() for t in TERMINAL_STATES},
}
VALID_STATES = frozenset(LEGAL_TRANSITIONS)


def _spawn_group(shell_command: str, **kwargs):
    """以独立进程组启动一条 bash 命令（固定参数列表，无字符串拼接）。"""
    return Popen(["/bin/bash", "-lc", shell_command], stdout=PIPE, stderr=PIPE,
                 start_new_session=True, **kwargs)


def validate_transition(old: str, new: str) -> None:
    if old not in LEGAL_TRANSITIONS:
        raise ValueError(f"unknown job state: {old!r}")
    if new not in LEGAL_TRANSITIONS:
        raise ValueError(f"unknown job state: {new!r}")
    if new not in LEGAL_TRANSITIONS[old]:
        raise ValueError(f"illegal job state transition: {old} -> {new}")


@dataclass
class JobSnapshot:
    """一次状态快照（invariant 矩阵的校验对象）。"""

    job_id: str
    kind: str
    state: str
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    signal: str | None = None
    timed_out: bool = False
    label: str = ""
    owner: str = ""

    def validate(self) -> None:
        """快照一致性（DSH jobs invariant 矩阵的 HAA 翻译）。

        非法快照必须在此抛错——注册表绝不存入自相矛盾的状态。
        """
        if not self.job_id or "-" not in self.job_id:
            raise ValueError(f"job_id must be '<kind>-<n>': {self.job_id!r}")
        kind = self.job_id.rsplit("-", 1)[0]
        if kind != self.kind:
            raise ValueError(f"job_id kind prefix mismatch: {self.job_id} vs {self.kind!r}")
        if self.state not in LEGAL_TRANSITIONS:
            raise ValueError(f"unknown state: {self.state!r}")
        if self.state == "running":
            if self.finished_at is not None:
                raise ValueError("running job must not carry finished_at")
        else:
            if self.finished_at is None:
                raise ValueError(f"terminal state {self.state!r} must carry finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at before started_at")
        if self.timed_out and self.state != "timeout":
            raise ValueError("timed_out flag only legal on timeout state")
        if self.state == "timeout" and not self.timed_out:
            raise ValueError("timeout state must carry timed_out=True")


class Job:
    """一个后台进程句柄：消费式输出读取 + 进程组击杀 + 状态机。"""

    GRACE_S = 5.0  # TERM→KILL 升级宽限（DSH graceMs 的 HAA 默认）

    def __init__(self, job_id: str, kind: str, proc,
                 *, label: str = "", owner: str = "",
                 emit: Callable[[str, str, str], None] | None = None,
                 max_buffer_bytes: int = 1_000_000):
        self.job_id = job_id
        self.kind = kind
        self.proc = proc
        self.label = label
        self.owner = owner
        self._emit = emit
        self.started_at = time.time()
        self.state = "running"
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.signal_name: str | None = None
        self.timed_out = False
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._trimmed = 0          # 因环形上限被丢弃的头部字节数（lossy 判定）
        self._delivered = 0        # 消费式读取指针
        self._max_buffer = max_buffer_bytes
        self._settled = threading.Event()
        self._pumps: list[threading.Thread] = []
        for stream, tag in ((proc.stdout, ""), (proc.stderr, "[stderr] ")):
            if stream is not None:
                t = threading.Thread(target=self._pump, args=(stream, tag), daemon=True)
                t.start()
                self._pumps.append(t)

    # -- 输出收集 -----------------------------------------------------------

    def _pump(self, stream, tag: str) -> None:
        try:
            for raw in iter(stream.readline, b""):
                with self._lock:
                    if tag:
                        self._buf += tag.encode() + raw
                    else:
                        self._buf += raw
                    if len(self._buf) > self._max_buffer:
                        drop = len(self._buf) - self._max_buffer
                        del self._buf[:drop]
                        self._trimmed += drop
        except Exception:  # noqa: BLE001 — 收集线程死亡不炸主流程，结算时可见
            logger.exception("output pump failed for %s", self.job_id)
        finally:
            self._maybe_settle()

    # -- 状态机 -------------------------------------------------------------

    def _set_state(self, new: str, *, exit_code=None, signal_name=None,
                   timed_out: bool = False) -> None:
        validate_transition(self.state, new)
        self.state = new
        self.finished_at = time.time()
        self.exit_code = exit_code
        self.signal_name = signal_name
        self.timed_out = timed_out
        if self._emit:
            try:
                self._emit(self.job_id, new, f"exit={exit_code} signal={signal_name}")
            except Exception:  # noqa: BLE001
                logger.exception("job/state emit failed for %s", self.job_id)
        self._settled.set()

    def _maybe_settle(self) -> bool:
        """进程退出后按退出形态结算（completed / killed）。"""
        if self.state != "running":
            return True
        rc = self.proc.poll()
        if rc is None:
            return False
        if rc < 0:
            self._set_state("killed", exit_code=None,
                            signal_name=f"SIG{signal.Signals(-rc).name}")
        else:
            self._set_state("completed", exit_code=rc)
        return True

    # -- 对外 API -----------------------------------------------------------

    def read(self, offset: int | None = None, limit: int = 60_000) -> tuple[str, int, bool]:
        """消费式读取：返回 (增量文本, 下次 offset, 是否有损)。

        不传 offset 从上次投递点继续——已读增量绝不重复投递；传早于环形
        起点的 offset → lossy=True（头部已被丢弃，只能给现存段）。
        进程死亡时顺带结算（读到退出状态而非挂起——§5.1 可靠性要求）；
        终态读前先等输出泵线程冲完（修"结算先于冲刷"竞态——退出后余量
        必须可读全）。
        """
        self._maybe_settle()
        if self.state != "running":
            for t in self._pumps:
                t.join(timeout=2.0)
        with self._lock:
            start = self._delivered if offset is None else max(int(offset), 0)
            buf_start = self._trimmed
            lossy = start < buf_start
            eff_start = max(start, buf_start)
            chunk = bytes(self._buf[eff_start - buf_start: eff_start - buf_start + limit])
            self._delivered = max(self._delivered, eff_start + len(chunk))
        text = chunk.decode("utf-8", errors="replace")
        return text, eff_start + len(chunk), lossy

    def tail(self, n: int = 2000) -> str:
        with self._lock:
            data = bytes(self._buf[-n:])
        return data.decode("utf-8", errors="replace")

    def kill(self, *, grace_s: float | None = None, reason: str = "job_kill") -> bool:
        """终止进程组：TERM → 宽限 → KILL 升级。幂等；已结算返回 False。"""
        if self.state != "running":
            return False
        grace = self.GRACE_S if grace_s is None else grace_s
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=grace)
        except Exception:  # noqa: BLE001 — 超时未退，升级 KILL
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self.state == "running":  # 收集线程尚未结算则就地结算
            rc = self.proc.poll()
            if rc is not None and rc < 0:
                self._set_state("killed", exit_code=None,
                                signal_name=f"SIG{signal.Signals(-rc).name}")
            else:
                self._set_state("killed", exit_code=rc, signal_name="SIGTERM")
        return True

    def mark_timeout(self) -> None:
        """超时击杀的带因结算（timeout 态，供监控区分死因）。"""
        if self.state == "running":
            self._set_state("timeout", timed_out=True, signal_name="SIGTERM")

    def wait(self, timeout_s: float | None = None) -> JobSnapshot:
        """阻塞到结算；超时返回当时快照（任务仍活——wait 帽语义）。"""
        if not self._settled.wait(timeout=timeout_s):
            self._maybe_settle()
        return self.snapshot()

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            job_id=self.job_id, kind=self.kind, state=self.state,
            started_at=self.started_at, finished_at=self.finished_at,
            exit_code=self.exit_code, signal=self.signal_name,
            timed_out=self.timed_out, label=self.label, owner=self.owner,
        )

    @property
    def alive(self) -> bool:
        return self.state == "running"


class JobManager:
    """注册表：id 发放（kind-N 每 kind 独立计数）、并发帽、隔离与查询。"""

    def __init__(self, *, max_concurrent_per_owner: int = 10,
                 emit: Callable[[str, str, str], None] | None = None):
        self._jobs: dict[str, Job] = {}
        self._counters: dict[str, int] = {}
        self._cap = max_concurrent_per_owner
        self._emit = emit
        self._lock = threading.Lock()

    def start(self, shell_command: str, *, kind: str = "bash", label: str = "",
              owner: str = "", cwd: str | None = None, env: dict | None = None) -> str:
        """启动一个后台 bash 任务（独立进程组）。"""
        with self._lock:
            active = sum(1 for j in self._jobs.values() if j.owner == owner and j.alive)
            if active >= self._cap:
                raise ToolError(
                    f"background job limit reached ({self._cap} active for this owner)"
                )
            n = self._counters.get(kind, 0) + 1
            self._counters[kind] = n
            job_id = f"{kind}-{n}"
        proc = _spawn_group(shell_command, cwd=cwd, env=env)
        job = Job(job_id, kind, proc, label=label, owner=owner, emit=self._emit)
        with self._lock:
            self._jobs[job_id] = job
        if self._emit:
            self._emit(job_id, "running", f"started label={label!r}")
        return job_id

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(f"unknown job {job_id!r}")
        return job

    def list(self, owner: str = "") -> list[JobSnapshot]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.snapshot() for j in jobs if j.owner in ("", owner)]

    def read(self, job_id: str, offset: int | None = None) -> tuple[str, JobSnapshot, int, bool]:
        job = self.get(job_id)
        text, nxt, lossy = job.read(offset)
        return text, job.snapshot(), nxt, lossy

    def kill(self, job_id: str, *, reason: str = "job_kill") -> str:
        job = self.get(job_id)
        if not job.alive:
            return f"job {job_id} already-finished ({job.state})"
        job.kill(reason=reason)
        return f"cancellation requested for {job_id} (reason: {reason})"
