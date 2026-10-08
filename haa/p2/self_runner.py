"""P2 自跑执行链（大修批次8，第四章 §3/§4——增补三件后完整版）。

DebugSession 改接：执行通道从 ACP-Claude Code 切换为新 Harness 工具循环
（exec_bash 读日志→edit_file 修码→重跑）；Phase A/B 断路器机制保留。
备用 ACP 开关接线（fallback_acp=true 时 Phase A 修复改走降级通道）。

增补三件（2026-10-08 用户指示补全）：
① 机型匹配检查——实验需求对照服务器/本地 Docker 资源，不匹配即警告
   （第四章 §3.1 第 1 步）；
② 概念档案代码位置回填——CODEGEN prompt 注入概念卡空 code_refs 清单，
   要求模型在生成代码时定位并回填（第四章 §3.1 第 3 步，AI-Researcher
   双向映射闭环）；
③ 实验设计冻结注入——p2_analyze.md 增 ⛓ 冻结纪律段，exp_spec/假设/
   成功判据以不可忽略的形态钉进分析上下文（第四章 §3.3，Finch 落点）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from haa.harness.registry import ToolRegistry
from haa.harness.tools.native import apply_native_tools

logger = logging.getLogger("haa.p2.self_runner")

# --------------------------------------------------------------------------- #
#  机型匹配检查（① 第四章 §3.1 第 1 步）
# --------------------------------------------------------------------------- #

# 本地 Docker 沙箱默认资源上限（保守估计，无 GPU）
_LOCAL_RESOURCES = {
    "gpu_memory_gb": 0,
    "cpu_cores": 8,
    "ram_gb": 16,
    "cuda_version": "",
    "disk_gb": 50,
}


def check_machine_match(exp_spec: dict, server_profile: Any = None,
                        *, local_mode: bool = True) -> list[str]:
    """实验需求对照执行环境资源——不匹配项列表（空=全匹配）。

    exp_spec 的 resource_estimate 字段（EXP_SPEC 产出）：
    {gpu_memory_gb, cpu_cores, ram_gb, data_size_gb, cuda_version}
    """
    req = exp_spec.get("resource_estimate") or {}
    if not req:
        return []  # 无需求声明 → 不拦

    if local_mode or server_profile is None:
        avail = dict(_LOCAL_RESOURCES)
    else:
        avail = {
            "gpu_memory_gb": getattr(server_profile, "gpu_memory_gb", 0),
            "cpu_cores": getattr(server_profile, "cpu_cores", 4),
            "ram_gb": getattr(server_profile, "ram_gb", 8),
            "cuda_version": getattr(server_profile, "cuda_version", ""),
            "disk_gb": getattr(server_profile, "disk_gb", 20),
        }

    warnings: list[str] = []
    for key, req_val in req.items():
        if isinstance(req_val, str):
            # CUDA 等版本字符串：需求 > 可用 → 警告
            avail_val = str(avail.get(key, ""))
            if req_val and avail_val and req_val > avail_val:
                warnings.append(
                    f"{key}: need {req_val}, have {avail_val}")
            continue
        if not isinstance(req_val, (int, float)) or req_val <= 0:
            continue
        avail_val = avail.get(key, 0)
        if isinstance(avail_val, str):
            continue
        if avail_val < req_val:
            unit = {"gpu_memory_gb": "GB GPU", "cpu_cores": "cores",
                    "ram_gb": "GB RAM", "data_size_gb": "GB disk"}.get(key, key)
            warnings.append(
                f"{key}: need {req_val} {unit}, have {avail_val}")
    if warnings:
        mode = "local docker sandbox" if local_mode else \
            f"server {getattr(server_profile, 'name', '?')}"
        logger.warning("machine mismatch (%s): %s", mode, "; ".join(warnings))
    return warnings


# --------------------------------------------------------------------------- #
#  概念档案回填（② 第四章 §3.1 第 3 步）
# --------------------------------------------------------------------------- #

def build_concept_backfill_block(concepts: list[dict]) -> str:
    """把 code_refs 为空的概念卡渲染成 CODEGEN prompt 注入段。"""
    empty = [c for c in (concepts or [])
             if isinstance(c, dict) and not c.get("code_refs")]
    if not empty:
        return ""
    lines = [
        "⛓⛓⛓ 概念档案回填义务 ⛓⛓⛓",
        "以下核心概念已在 DESIGN 阶段形式化定义，但尚未定位到代码实现。",
        "你生成的实验代码**必须**让每个概念有对应的代码位置——生成后",
        "在输出 JSON 中回填 code_refs（每条格式 {repo, path, symbol}）。",
        "",
    ]
    for c in empty:
        lines.append(f"- **{c.get('name', '?')}** ({c.get('concept_id', '?')})："
                     f"{c.get('math_formulation', '?')[:200]}")
    lines.append("")
    lines.append(
        '输出 JSON 顶层增加 "code_refs_backfill" 字段：')
    lines.append('```json')
    lines.append('{"code_refs_backfill": {"C1": [{"repo": "experiment", '
                  '"path": "code/range_txn.py", "symbol": "RangeTxn"}]}}')
    lines.append('```')
    return "\n".join(lines)


def parse_code_refs_backfill(text: str) -> dict[str, list[dict]]:
    """从 CODEGEN 输出解析 code_refs 回填（JSON 在最终文本里）。"""
    try:
        # 提取首个平衡的 JSON 块（code_refs_backfill 键所在的外层）
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    block = text[start:i + 1]
                    data = json.loads(block)
                    refs = data.get("code_refs_backfill")
                    if isinstance(refs, dict):
                        return {k: v for k, v in refs.items()
                                if isinstance(v, list)}
                    break  # 第一个平衡块里没有就不再找
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def backfill_concepts(concepts: list[dict],
                      backfill: dict[str, list[dict]]) -> list[dict]:
    """把回填结果写回概念卡（in-place，返回修改后的列表）。"""
    for card in concepts:
        if not isinstance(card, dict):
            continue
        cid = card.get("concept_id", "")
        refs = backfill.get(cid)
        if refs and not card.get("code_refs"):
            card["code_refs"] = refs
            logger.info("concept %s (%s) backfilled with %d code_ref(s)",
                        cid, card.get("name", "?"), len(refs))
    return concepts


# --------------------------------------------------------------------------- #
#  CODEGEN prompt（含概念回填段）
# --------------------------------------------------------------------------- #

CODEGEN_PROMPT = """You are a research experiment code generator for HAA (P2 CODE_GEN stage).

## Paper Precursor
Title: {title}

## Experiment Specification
```json
{exp_spec}
```

{machine_warning}{concept_backfill}

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
    """P2 自跑代码生成/修复（新 Harness 工具循环——D2 默认路径）。

    增补三件（2026-10-08）：
    - generate_code 前置机型匹配检查（§3.1 第 1 步）
    - generate_code 注入概念回填段 + 生成后解析回写（§3.1 第 3 步）
    - 分析环节的冻结注入由 p2_analyze.md 承载（§3.3，见该文件）
    """

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
        self.last_machine_warnings: list[str] = []

    def _loop(self):
        from haa.harness.agent_loop import AgentLoop
        return AgentLoop(None, self.registry, event_sink=self.event_sink)

    def generate_code(self, work_dir: str | Path, paper: dict, exp_spec: dict,
                      *, llm_client, concepts: list[dict] | None = None,
                      server_profile: Any = None) -> dict[str, Any]:
        """CODE_GEN：用 agent 循环生成 solve.sh 项目（替代 ACP generate_code）。

        增补：①机型匹配（前置警告） ②概念回填（注入+解析+回写）。
        """
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)

        # ① 机型匹配检查（§3.1 第 1 步）
        machine_warnings = check_machine_match(
            exp_spec, server_profile,
            local_mode=server_profile is None)
        self.last_machine_warnings = machine_warnings
        warning_block = ""
        if machine_warnings:
            warning_block = (
                "## ⚠ Machine Compatibility Warnings\n"
                + "\n".join(f"- {w}" for w in machine_warnings)
                + "\n\nConsider reducing data scale or using synthetic data.\n"
            )

        # ② 概念档案回填段（§3.1 第 3 步）
        concept_block = build_concept_backfill_block(concepts or [])

        prompt = CODEGEN_PROMPT.format(
            title=paper.get("candidate_title", paper.get("title", "(unknown)")),
            exp_spec=json.dumps(exp_spec, indent=2, default=str, ensure_ascii=False),
            work_dir=work,
            machine_warning=warning_block,
            concept_backfill=concept_block,
        )
        loop = self._loop()
        loop.client = llm_client
        result = loop.run(prompt, stage_name="CODE_GEN",
                          campaign_id="", max_tool_calls=30, json_mode=False)

        # ② 解析回填并回写概念卡
        backfill = parse_code_refs_backfill(result.content)
        updated_concepts = backfill_concepts(concepts or [], backfill) \
            if concepts else []

        return {"success": (work / "solve.sh").exists(),
                "output": result.content, "work_dir": str(work),
                "machine_warnings": machine_warnings,
                "code_refs_backfilled": len(backfill),
                "concepts": updated_concepts}

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
