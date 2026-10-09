"""DebugSession — P2 EXECUTE 内部的双阶段断路器（v0.7）。

管理本地编码 ↔ 远程执行的往复循环。两个阶段严格分离：

**Phase A（硬错误消除）**：
  - 特征：代码立即崩——import 错 / 维度不匹配 / CUDA OOM / Traceback
  - 循环：上传→跑→traceback→Coding Agent 修→重传
  - 断路器：``max_hard_error_rounds``（默认 15）
  - 时间：秒~分钟级

      ↓ A→B 检查点（v0.7 auto-approve；v0.8 加人工确认）

**Phase B（逻辑错误消除 + 调参）**：
  - 特征：代码能跑但结果不对——loss 发散 / 精度差 / 与理论矛盾
  - 循环：上传→跑(完整训练)→下载指标→Coding Agent+HAA 诊断→修→重传
  - 断路器：``max_logic_error_rounds``（默认 5）+ 早停
  - 时间：小时~天级

设计要点（HM-Pro 教训 + ACP 迁移参考）：
- Coding Agent 是无状态的——每轮只给"traceback/指标 + 当前代码"，返回修复。
- HAA 掌控循环——Coding Agent 不能决定"是否继续"。
- 断路器防止"旷日持久"——达到上限即 MORIBUND。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from haa.p2.transport import ExecutionTransport, RunResult

logger = logging.getLogger("haa.p2.debug")


# --------------------------------------------------------------------------- #
#  配置 + 结果
# --------------------------------------------------------------------------- #

@dataclass
class DebugConfig:
    """DebugSession 断路器参数（对应 ProjectHyperparams 的 P2 部分）。"""

    max_hard_error_rounds: int = 15
    max_logic_error_rounds: int = 5
    single_run_walltime_cap_s: int = 86400  # 24h
    early_stop_patience: int = 5  # loss 连续 N 轮发散 → 早停
    entry_command: str = "bash run.sh"
    auto_approve_a_to_b: bool = True  # v0.7：自动通过 A→B 检查点


@dataclass
class DebugResult:
    """DebugSession.run() 的返回值。"""

    success: bool
    phase: str = ""  # "phase_a" | "phase_b" | "done"
    rounds_a: int = 0
    rounds_b: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    log: str = ""
    error: str = ""
    reason: str = ""  # failure reason if not success
    results_dir: Path | None = None

    @property
    def circuit_breaker_tripped(self) -> bool:
        return not self.success and "circuit_breaker" in self.reason


# --------------------------------------------------------------------------- #
#  DebugSession
# --------------------------------------------------------------------------- #

class DebugSession:
    """管理 P2 EXECUTE 的双阶段调试循环。

    Parameters
    ----------
    transport:
        :class:`ExecutionTransport`——代码执行后端（Local/SSH）。
    coding_agent:
        :class:`~haa.coding_agent.ClaudeCodeACP`——代码修复服务。
        必须已绑定到正确的工作目录。
    config:
        :class:`DebugConfig`——断路器参数。
    code_dir:
        本地代码目录（每轮从这里 deploy 到执行环境）。
    work_dir:
        DebugSession 的工作目录（下载结果、临时文件）。
    campaign_id:
        用于日志 + run_id 隔离。
    event_sink:
        A4 批次18-2：可选事件回流回调 ``(event_type, payload) -> None``。
        选回调而非直传 store：DebugSession 与存储层解耦（transport /
        coding_agent 同为注入边界），回调可测试注入且无需知道
        ``save_event`` 签名。project_controller 注入
        ``store.save_event`` 适配层。发射失败吞掉——事件层不阻断调试循环。
    """

    def __init__(
        self,
        transport: ExecutionTransport,
        coding_agent: Any,
        config: DebugConfig,
        code_dir: str | Path,
        work_dir: str | Path,
        campaign_id: str,
        event_sink: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.transport = transport
        self.coding_agent = coding_agent
        self.config = config
        self.code_dir = Path(code_dir)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.campaign_id = campaign_id
        self.event_sink = event_sink

    def _emit(self, event_type: str, payload: dict) -> None:
        """A4 批次18-2：经 event_sink 发事件（无 cost；失败仅记日志）。"""
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, payload)
        except Exception as exc:  # noqa: BLE001 — 可观测层不得阻断调试
            logger.warning("event_sink(%s) failed: %s", event_type, exc)

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #

    def run(self) -> DebugResult:
        """执行完整 DebugSession。

        Phase A → (A→B 检查点) → Phase B → done / circuit breaker。
        """
        logger.info("DebugSession %s: starting (code_dir=%s)", self.campaign_id, self.code_dir)

        # --- Phase A: hard error elimination ---
        phase_a = self._run_phase_a()
        if not phase_a.success:
            return phase_a

        # --- A→B checkpoint ---
        if not self.config.auto_approve_a_to_b:
            logger.info("DebugSession %s: A→B checkpoint (manual review needed)", self.campaign_id)
            # v0.7: auto-approve. v0.8 will add a pause mechanism here.
        else:
            logger.info("DebugSession %s: A→B checkpoint auto-approved", self.campaign_id)

        # --- Phase B: logic error elimination ---
        phase_b = self._run_phase_b(phase_a.rounds_a)
        return phase_b

    # ------------------------------------------------------------------ #
    #  Phase A: 硬错误消除
    # ------------------------------------------------------------------ #

    def _run_phase_a(self) -> DebugResult:
        """循环：deploy → run → 分诊 → 修复/重试（批次18 接线：分诊+HOLD）。"""
        from haa.p2revise import triage_failure, check_hold_flag, set_hold_flag, build_assist_request
        for round_num in range(1, self.config.max_hard_error_rounds + 1):
            # 轮间检查点（§2.3.2）：HOLD 标志命中→当前轮跑完安全退出
            if check_hold_flag(self.campaign_id):
                logger.info("DebugSession %s: HOLD detected at round %d — safe exit",
                            self.campaign_id, round_num)
                return DebugResult(
                    success=False, phase="phase_a", rounds_a=round_num,
                    reason="hold_detected",
                    error="User HOLD detected between rounds")

            run_result = self._execute_round(round_num, phase="a")

            if not run_result.crashed:
                logger.info(
                    "DebugSession %s: Phase A passed in %d round(s)",
                    self.campaign_id, round_num,
                )
                return DebugResult(
                    success=True,
                    phase="phase_a",
                    rounds_a=round_num,
                    metrics=self._parse_metrics(run_result),
                    log=run_result.combined_log,
                    results_dir=run_result.results_dir,
                )

            # 批次18 挂点1：根因分诊器（§2.2——先分诊再修复）
            verdict = triage_failure(run_result.combined_log)
            logger.info("DebugSession %s: triage → %s (%s)",
                        self.campaign_id, verdict["category"],
                        "; ".join(verdict["evidence_lines"][:1]))
            # A4 批次18-2：每轮分诊事件回流（payload=category+evidence_lines）
            self._emit("triage_verdict", {
                "category": verdict["category"],
                "evidence_lines": list(verdict["evidence_lines"][:3]),
                "phase": "phase_a", "round": round_num,
            })
            if verdict["category"] == "assist":
                # §2.3.4：协助类自动 HOLD（三要素求助请求）
                set_hold_flag(self.campaign_id,
                              note=build_assist_request(verdict))
                logger.warning("DebugSession %s: auto-HOLD (assist)", self.campaign_id)
                return DebugResult(
                    success=False, phase="phase_a", rounds_a=round_num,
                    reason="assist_hold",
                    error=build_assist_request(verdict))
            if verdict["category"] == "environment":
                # §2.2.1：环境类→退出循环走环境重配
                logger.warning("DebugSession %s: environment issue detected",
                               self.campaign_id)
                return DebugResult(
                    success=False, phase="phase_a", rounds_a=round_num,
                    reason="environment_issue",
                    error=run_result.combined_log[-500:])
            # code/design → 修复循环继续

            # Hard error → fix.
            logger.warning(
                "DebugSession %s: Phase A round %d crashed (rc=%d), requesting fix",
                self.campaign_id, round_num, run_result.exit_code,
            )
            self._request_fix(run_result.combined_log)

        # Circuit breaker tripped.
        logger.error(
            "DebugSession %s: Phase A circuit breaker tripped (%d rounds)",
            self.campaign_id, self.config.max_hard_error_rounds,
        )
        return DebugResult(
            success=False,
            phase="phase_a",
            rounds_a=self.config.max_hard_error_rounds,
            reason="phase_a_circuit_breaker",
            error=f"Failed to eliminate hard errors in {self.config.max_hard_error_rounds} rounds",
        )

    # ------------------------------------------------------------------ #
    #  Phase B: 逻辑错误消除
    # ------------------------------------------------------------------ #

    def _run_phase_b(self, rounds_a: int) -> DebugResult:
        """循环：deploy → run(完整) → 下载指标 → 检查合理性 → CC 诊断 → 重试。"""
        prev_loss: float | None = None
        diverge_count = 0

        for round_num in range(1, self.config.max_logic_error_rounds + 1):
            run_result = self._execute_round(round_num, phase="b")

            if run_result.crashed:
                # Code that worked in Phase A is now crashing — treat as logic-round hard error.
                logger.warning(
                    "DebugSession %s: Phase B round %d crashed unexpectedly",
                    self.campaign_id, round_num,
                )
                self._request_fix(run_result.combined_log)
                continue

            metrics = self._parse_metrics(run_result)
            loss = self._extract_loss(metrics)

            # Early stop: loss diverging for N consecutive rounds.
            if loss is not None and prev_loss is not None and loss > prev_loss * 1.5:
                diverge_count += 1
                if diverge_count >= self.config.early_stop_patience:
                    logger.error(
                        "DebugSession %s: Phase B early-stop (loss diverged %d rounds)",
                        self.campaign_id, diverge_count,
                    )
                    return DebugResult(
                        success=False,
                        phase="phase_b",
                        rounds_a=rounds_a,
                        rounds_b=round_num,
                        metrics=metrics,
                        reason="phase_b_early_stop",
                        error=f"Loss diverged for {diverge_count} consecutive rounds",
                    )
            else:
                diverge_count = 0
            prev_loss = loss

            # Check if metrics look reasonable.
            if self._metrics_look_reasonable(metrics):
                logger.info(
                    "DebugSession %s: Phase B passed in %d round(s)",
                    self.campaign_id, round_num,
                )
                return DebugResult(
                    success=True,
                    phase="done",
                    rounds_a=rounds_a,
                    rounds_b=round_num,
                    metrics=metrics,
                    log=run_result.combined_log,
                    results_dir=run_result.results_dir,
                )

            # Logic error → CC diagnose.
            logger.info(
                "DebugSession %s: Phase B round %d — metrics anomalous, requesting diagnosis",
                self.campaign_id, round_num,
            )
            self._request_diagnosis(metrics, run_result.combined_log)

        # Circuit breaker tripped.
        logger.error(
            "DebugSession %s: Phase B circuit breaker tripped (%d rounds)",
            self.campaign_id, self.config.max_logic_error_rounds,
        )
        return DebugResult(
            success=False,
            phase="phase_b",
            rounds_a=rounds_a,
            rounds_b=self.config.max_logic_error_rounds,
            reason="phase_b_circuit_breaker",
            error=f"Failed to produce reasonable metrics in {self.config.max_logic_error_rounds} rounds",
        )

    # ------------------------------------------------------------------ #
    #  单轮执行
    # ------------------------------------------------------------------ #

    def _execute_round(self, round_num: int, *, phase: str) -> RunResult:
        """一轮 deploy → run → download。"""
        run_id = f"{self.campaign_id}_{phase}_r{round_num}"
        exec_dir = self.transport.deploy(self.code_dir, run_id)
        run_result = self.transport.run(
            exec_dir,
            self.config.entry_command,
            timeout=self.config.single_run_walltime_cap_s,
        )
        # Download results.
        results_dir = self.work_dir / run_id / "results"
        try:
            self.transport.download_results(exec_dir, results_dir)
            run_result.results_dir = results_dir
        except Exception as exc:
            logger.warning("DebugSession: results download failed: %s", exc)
        return run_result

    # ------------------------------------------------------------------ #
    #  Coding Agent 交互
    # ------------------------------------------------------------------ #

    def _request_fix(self, error_log: str) -> None:
        """请求 Coding Agent 修复 traceback。"""
        try:
            self.coding_agent.fix_traceback(error_log)
        except Exception as exc:
            logger.error("Coding agent fix_traceback failed: %s", exc)

    def _request_diagnosis(self, metrics: dict, log: str) -> None:
        """请求 Coding Agent 诊断异常指标并修代码。"""
        try:
            self.coding_agent.diagnose_metrics(metrics, log)
        except Exception as exc:
            logger.error("Coding agent diagnose_metrics failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  指标解析 + 合理性检查
    # ------------------------------------------------------------------ #

    def _parse_metrics(self, run_result: RunResult) -> dict[str, Any]:
        """从下载的 results/metrics.json 解析指标。"""
        if run_result.results_dir is None:
            return {}
        metrics_file = run_result.results_dir / "metrics.json"
        if not metrics_file.exists():
            return {}
        try:
            return json.loads(metrics_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("metrics.json parse failed: %s", exc)
            return {}

    @staticmethod
    def _extract_loss(metrics: dict[str, Any]) -> float | None:
        """从指标中提取 loss 值（支持多种键名）。"""
        for key in ("final_loss", "loss", "train_loss", "val_loss"):
            val = metrics.get(key)
            if isinstance(val, (int, float)) and not (math.isnan(val) or math.isinf(val)):
                return float(val)
        return None

    def _metrics_look_reasonable(self, metrics: dict[str, Any]) -> bool:
        """启发式检查指标是否合理。

        保守策略：只要有非 NaN/Inf 的指标且没有明显的失败标志就算通过。
        具体阈值留给后续 P2 调优（需真实实验数据校准）。
        """
        if not metrics:
            # No metrics file → can't confirm success → keep debugging.
            return False
        # NaN / Inf anywhere → failure.
        for val in metrics.values():
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return False
        # Explicit failure flag.
        if metrics.get("status") in ("failed", "error", "diverged"):
            return False
        return True
