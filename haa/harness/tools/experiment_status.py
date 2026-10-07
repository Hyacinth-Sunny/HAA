"""工具⑤：运行监控（experiment_status）——统一状态模型与心跳判定。

大修第一章 §5.5 规格，行为规格来自 DSH 测试翻译清单 §5：
- 本地后台任务与远端实验**统一状态模型**（同一套状态词与事件）：
  running → completed | failed | killed | timeout（终态不回退，
  :mod:`haa.harness.tools.jobs` 承载迁移合法性校验）；
- 状态变化写 ``job/state`` 事件（JobManager 发射，本工具查询呈现）；
- 心跳丢失判定：连续 3 次轮询无状态进展且无日志增长 → 判死收尸；
- 轮询周期默认 60 秒；日志尾部按需拉取（暴涨截断纪律同工具结果）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.exec_bash import truncate_output
from haa.harness.tools.jobs import TERMINAL_STATES

logger = logging.getLogger("haa.harness.tools.monitor")

DEFAULT_POLL_INTERVAL_S = 60
HEARTBEAT_LOSS_THRESHOLD = 3
DEFAULT_LOG_TAIL_LINES = 50
DEFAULT_LOG_TAIL_CHARS = 4000


@dataclass
class RemoteExperiment:
    """远端实验登记项（M2 run_experiment 接入；M1 提供状态查询面）。"""

    experiment_id: str
    server: str
    command: str
    state: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log_tail_path: str = ""

    def snapshot(self):
        from haa.harness.tools.jobs import JobSnapshot
        return JobSnapshot(
            job_id=self.experiment_id, kind="experiment", state=self.state,
            started_at=self.started_at, finished_at=self.finished_at,
            label=self.command[:80], owner=f"remote:{self.server}",
        )


@dataclass
class HeartbeatState:
    """一次被监控对象的心跳账目（progress = 状态或输出长度的变化证据）。"""

    last_state: str = "running"
    last_progress_marker: int = -1
    stale_polls: int = 0


class HeartbeatMonitor:
    """心跳丢失判定（第四章 §6：连续 3 次无进展无日志增长 → 判死）。"""

    def __init__(self, threshold: int = HEARTBEAT_LOSS_THRESHOLD):
        self.threshold = max(1, int(threshold))
        self._states: dict[str, HeartbeatState] = {}

    def observe(self, job_id: str, *, state: str, progress_marker: int) -> bool:
        """记录一次轮询观测。返回 True=心跳丢失（判死，应收尸）。"""
        st = self._states.get(job_id)
        if st is None:
            self._states[job_id] = HeartbeatState(state, progress_marker, 0)
            return False
        progressed = (state != st.last_state) or (progress_marker != st.last_progress_marker)
        st.last_state = state
        st.last_progress_marker = progress_marker
        st.stale_polls = 0 if progressed else st.stale_polls + 1
        return st.stale_polls >= self.threshold and state == "running"

    def reset(self, job_id: str) -> None:
        self._states.pop(job_id, None)


@dataclass
class CorpseReport:
    """收尸报告（最后日志尾部＋已耗时长——第四章 §6）。"""

    job_id: str
    ran_s: float
    last_log_tail: str
    reason: str = "heartbeat lost (3 consecutive polls without progress)"


class MonitorService:
    """本地 JobManager ＋ 远端实验登记的统一查询/监控面。"""

    def __init__(self, jobs, *, remote_registry: dict[str, RemoteExperiment] | None = None,
                 poll_interval_s: int = DEFAULT_POLL_INTERVAL_S):
        self.jobs = jobs
        self.remote: dict[str, RemoteExperiment] = dict(remote_registry or {})
        self.heartbeat = HeartbeatMonitor()
        self.poll_interval_s = poll_interval_s

    def lookup(self, job_id: str):
        """统一查找：先本地后台任务，再远端实验登记。"""
        try:
            return ("local", self.jobs.get(job_id))
        except ToolError:
            exp = self.remote.get(job_id)
            if exp is None:
                raise ToolError(f"unknown job or experiment {job_id!r}")
            return ("remote", exp)

    def status(self, job_id: str, *, log_tail: bool = True) -> dict:
        kind, obj = self.lookup(job_id)
        snap = obj.snapshot()
        out: dict[str, Any] = {
            "job_id": snap.job_id, "kind": kind, "state": snap.state,
            "started_at": snap.started_at, "finished_at": snap.finished_at,
            "exit_code": snap.exit_code, "signal": snap.signal,
            "terminal": snap.state in TERMINAL_STATES,
            "elapsed_s": (snap.finished_at or time.time()) - snap.started_at,
        }
        if log_tail:
            out["log_tail"] = self.log_tail(job_id)
        # 心跳观测（本地任务以已投递输出长度为进展证据；远端以登记状态）
        marker = getattr(obj, "_delivered", None)
        marker = marker if isinstance(marker, int) else hash(snap.state)
        out["heartbeat_lost"] = self.heartbeat.observe(
            job_id, state=snap.state, progress_marker=int(marker)
        )
        return out

    def log_tail(self, job_id: str, *, lines: int = DEFAULT_LOG_TAIL_LINES) -> str:
        kind, obj = self.lookup(job_id)
        if kind == "local":
            tail = obj.tail(n=DEFAULT_LOG_TAIL_CHARS)
        else:
            tail = f"(remote log — fetch via ssh_exec tail -n {lines} {obj.log_tail_path})" \
                if obj.log_tail_path else "(no remote log path registered)"
        bounded, _ = truncate_output(tail, DEFAULT_LOG_TAIL_CHARS)
        return bounded

    def collect_corpse(self, job_id: str) -> CorpseReport:
        """判死后的收尸：终止进程组（本地）＋生成收尸报告。"""
        kind, obj = self.lookup(job_id)
        if kind == "local":
            if obj.alive:
                obj.kill(reason="heartbeat lost")
            snap = obj.snapshot()
            ran = (snap.finished_at or time.time()) - snap.started_at
            report = CorpseReport(job_id, ran, self.log_tail(job_id))
        else:
            ran = (obj.finished_at or time.time()) - obj.started_at
            report = CorpseReport(job_id, ran, self.log_tail(job_id))
            obj.state = "killed"  # 远端收尸由调用方经 ssh_exec 执行
            obj.finished_at = time.time()
        self.heartbeat.reset(job_id)
        return report


def make_handlers(services) -> dict[str, Any]:
    """experiment_status 工具 handler。"""

    def experiment_status(args: dict, ctx: ToolCallContext) -> ToolResult:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            raise ToolError("experiment_status: 'job_id' must be non-empty")
        info = services.monitor.status(job_id)
        lines = [
            f"job: {info['job_id']} ({info['kind']})",
            f"state: {info['state']}" + (" (terminal)" if info["terminal"] else ""),
            f"elapsed: {info['elapsed_s']:.0f}s",
        ]
        if info.get("exit_code") is not None:
            lines.append(f"exit code: {info['exit_code']}")
        if info.get("signal"):
            lines.append(f"signal: {info['signal']}")
        if info.get("heartbeat_lost"):
            lines.append("HEARTBEAT LOST — 3 consecutive polls without progress; "
                         "consider collecting a corpse report")
        if args.get("log_tail", True):
            lines.append("---- log tail ----")
            lines.append(info.get("log_tail", ""))
        body, _ = truncate_output("\n".join(lines))
        return ToolResult(content=body, meta=info)

    return {"experiment_status": experiment_status}


MONITOR_TOOL_RULES = (
    "Long tasks: poll experiment_status (default every 60s) instead of "
    "blocking; 'HEARTBEAT LOST' means the task is presumed dead — collect a "
    "corpse report rather than waiting forever."
)
