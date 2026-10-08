"""P2 自跑执行链（大修批次8，第四章 §3/§4）。

DebugSession 改接：执行通道从 ACP-Claude Code 切换为新 Harness 工具循环
（exec_bash 读日志→edit_file 修码→重跑）；Phase A/B 断路器机制保留。
备用 ACP 开关接线（fallback_acp=true 时 Phase A 修复改走降级通道）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from haa.harness.registry import ToolRegistry
from haa.harness.tools.native import apply_native_tools

logger = logging.getLogger("haa.p2.self_runner")

CODEGEN_PROMPT = """You are a research experiment code generator for HAA (P2 CODE_GEN stage).

## Paper Precursor
Title: {title}

## Experiment Specification
```json
{exp_spec}
```

## Working Directory: {work_dir}

## Your Task
Generate a COMPLETE, runnable Python experiment project. The entry point MUST be
`solve.sh` (bash script) that runs the whole experiment. Project structure:

```
solve.sh          # Entry: bash solve.sh
README.md         # Brief description + expected runtime
code/             # Python source files
data/             # Data files or generation scripts
output/           # Created by solve.sh; must contain metrics.json
  metrics.json    # {{metric_name: value}} flat JSON (REQUIRED)
  logs/           # error.log if failed
```

## Rules
- Use `write_file` tool to create each file in the working directory
- solve.sh must: `cd "$(dirname "$0")" && python code/main.py`
- Exit code 0 = success; non-zero = failure
- If data unavailable, generate synthetic data
- Write results to `output/metrics.json` (flat dict of numbers)
- Use `exec_bash` to verify: `bash solve.sh` (quick check)
"""

FIX_PROMPT = """The experiment code crashed with the following traceback:

```
{traceback}
```

Working directory: {work_dir}

Fix the root cause in the current workspace. Use `read_file` to inspect the
relevant source files, then `edit_file` to apply fixes. Do NOT silence or
catch-and-ignore errors — fix the root cause.
"""

DIAGNOSE_PROMPT = """The experiment code runs but results are anomalous.

## Metrics
```json
{metrics}
```

## Log tail
```
{log_tail}
```

Working directory: {work_dir}

Diagnose the likely cause and fix the code. Common causes: loss divergence,
wrong learning rate, data preprocessing bugs, gradient explosion/vanishing,
incorrect metric computation. Fix the most likely root cause.
"""


class SelfRunningCoder:
    """P2 自跑代码生成/修复（新 Harness 工具循环——D2 默认路径）。"""

    def __init__(self, *, campaigns_dir: str | Path, config=None,
                 event_sink=None):
        self.registry = ToolRegistry()
        apply_native_tools(
            self.registry,
            allowed_roots=(Path(campaigns_dir),),
            ssh_profiles=dict(getattr(getattr(config, "harness", None),
                                       "ssh_servers", None) or {}),
        )
        self.config = config
        self.event_sink = event_sink

    def _loop(self):
        from haa.harness.agent_loop import AgentLoop
        return AgentLoop(None, self.registry, event_sink=self.event_sink)

    def generate_code(self, work_dir: str | Path, paper: dict, exp_spec: dict,
                      *, llm_client) -> dict[str, Any]:
        """CODE_GEN：用 agent 循环生成 solve.sh 项目（替代 ACP generate_code）。"""
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        prompt = CODEGEN_PROMPT.format(
            title=paper.get("candidate_title", paper.get("title", "(unknown)")),
            exp_spec=json.dumps(exp_spec, indent=2, default=str, ensure_ascii=False),
            work_dir=work,
        )
        loop = self._loop()
        loop.client = llm_client
        result = loop.run(prompt, stage_name="CODE_GEN",
                          campaign_id="", max_tool_calls=30, json_mode=False)
        return {"success": (work / "solve.sh").exists(),
                "output": result.content, "work_dir": str(work)}

    def fix_traceback(self, work_dir: str | Path, error_log: str,
                      *, llm_client) -> dict[str, Any]:
        """Phase A：读 traceback → edit_file 修码 → 返回修复描述。"""
        prompt = FIX_PROMPT.format(traceback=error_log[-3000:],
                                   work_dir=work_dir)
        loop = self._loop()
        loop.client = llm_client
        result = loop.run(prompt, stage_name="P2_DEBUG_A",
                          campaign_id="", max_tool_calls=15, json_mode=False)
        return {"success": True, "output": result.content}

    def diagnose_metrics(self, work_dir: str | Path, metrics: dict,
                         log_tail: str, *, llm_client) -> dict[str, Any]:
        """Phase B：分析异常指标 → 修码。"""
        prompt = DIAGNOSE_PROMPT.format(
            metrics=json.dumps(metrics, indent=2, default=str),
            log_tail=log_tail[-2000:],
            work_dir=work_dir,
        )
        loop = self._loop()
        loop.client = llm_client
        result = loop.run(prompt, stage_name="P2_DEBUG_B",
                          campaign_id="", max_tool_calls=15, json_mode=False)
        return {"success": True, "output": result.content}
