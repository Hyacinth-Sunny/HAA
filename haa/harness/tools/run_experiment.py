"""M2 核心件：run_experiment——以 solve.sh 为唯一入口在沙箱跑实验方案。

大修第一章 §5/§4.2 与第四章 §5（solve.sh 统一入口契约）：
- 目录契约：<workspace>/solve.sh 必在；产出 output/metrics.json（平铺
  {metric: value}）、logs/（含 error.log）、figures/；
- 退出码语义：0=成功；非 0=失败，原因在 stderr 尾部与 logs/error.log；
- 执行模式：container=True（默认，P2 全量口径）经 DockerSandbox（默认
  禁网、工作区可写挂载）；container=False 走轻量沙箱（前台 bash，同
  32KB/超时/退出码标记纪律）；
- 结果结构化：metrics.json 解析进 meta；日志尾部截断随行；四路判决的
  判定交给调用方（PILOT 微阶段 / P2 ANALYZE）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.exec_bash import truncate_output
from haa.harness.tools.sandbox import DockerSandbox

logger = logging.getLogger("haa.harness.tools.run_experiment")

RUN_EXPERIMENT_RULES = (
    "Experiments: package every experiment as a directory whose ONLY entry is "
    "solve.sh (exit 0 = success; write output/metrics.json as flat "
    "{metric: value}). run_experiment executes it in a sandbox and returns "
    "parsed metrics plus the log tail — check the exit status before "
    "analyzing."
)


def _read_metrics(workspace: Path) -> dict:
    p = workspace / "output" / "metrics.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"_raw": str(data)[:500]}
    except (OSError, ValueError) as exc:
        return {"_metrics_parse_error": str(exc)[:200]}


def _tail(path: Path, n: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def make_handlers(services) -> dict[str, Any]:

    def run_experiment(args: dict, ctx: ToolCallContext) -> ToolResult:
        raw = str(args.get("workspace", "")).strip()
        if not raw:
            raise ToolError("run_experiment: 'workspace' (dir containing solve.sh) is required")
        workspace = Path(raw).expanduser()
        if not services.path_allowed(str(workspace)):
            raise ToolError(f"run_experiment: workspace outside sandbox roots: {raw}")
        solve = workspace / "solve.sh"
        if not solve.exists():
            raise ToolError(
                f"run_experiment: solve.sh not found in {raw} — every experiment "
                "must be runnable via its solve.sh entry (Ch4 §5)")
        try:
            timeout_s = max(5, min(int(args.get("timeout_s", 600)), 7200))
        except (TypeError, ValueError):
            timeout_s = 600
        use_container = bool(args.get("container", True))
        cmd = "bash solve.sh"
        if use_container:
            sandbox = services.docker
            res = sandbox.run(cmd, str(workspace), timeout_s=timeout_s,
                              writable=True, network=None)
            rc, out, err = res.returncode, res.stdout, res.stderr
        else:
            fg = services.foreground
            r = fg.run(cmd, cwd=str(workspace), timeout_ms=timeout_s * 1000)
            rc = r.meta.get("exit_code")
            out, err = r.content, ""
        metrics = _read_metrics(workspace)
        log_tail = ""
        errlog = workspace / "output" / "logs" / "error.log"
        if not errlog.exists():
            errlog = workspace / "logs" / "error.log"
        if errlog.exists():
            log_tail = _tail(errlog)
        ok = rc == 0 and bool(metrics)
        lines = [
            f"experiment finished: exit={'0' if rc == 0 else str(rc)} "
            f"({'success' if rc == 0 else 'FAILED — see error tail'})",
        ]
        if metrics:
            lines.append("metrics: " + json.dumps(metrics, ensure_ascii=False)[:2000])
        else:
            lines.append("metrics: (output/metrics.json missing or empty)")
        tail_src = (err or out or log_tail).strip()
        if tail_src:
            lines.append("---- output/error tail ----")
            body, _ = truncate_output(tail_src, 4000)
            lines.append(body)
        text, _ = truncate_output("\n".join(lines), 8000)
        return ToolResult(content=text, meta={
            "exit_code": rc, "metrics": metrics, "ok": ok,
            "mode": "container" if use_container else "lightweight",
        })

    return {"run_experiment": run_experiment}
