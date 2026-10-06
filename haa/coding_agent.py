"""ClaudeCodeACP — HAA↔Claude Code 编码通道（v0.6）。

通过 ``acpx`` CLI（Agent Client Protocol 客户端）调用 Claude Code，
将代码生成/调试任务委托给专业编码 Agent。HAA pipeline 保持状态机控制权，
Claude Code 是无状态的代码生成/修复服务。

架构（详见《Coding Agent ACP迁移参考.md》）::

    HAA Pipeline → subprocess acpx CLI → Claude Agent ACP → Claude Code

关键设计：
- **每个 campaign 独立会话**（session 名 = ``{prefix}-{campaign_id}``），
  避免不同 campaign 的代码上下文互相污染。
- **prompt 写文件用 ``-f`` 发送**，避免 shell 转义问题。
- **要求 CC 把最终代码写入指定目录**，HAA 从文件读取，不解析 stdout。
- **失败降级**：超时/断开/限额 → 可选降级到 LiteLLM AgentLoop（``fallback_to_agentloop``）。
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haa.config import ACPConfig

logger = logging.getLogger("haa.acp")


# --------------------------------------------------------------------------- #
#  acpx 路径探测
# --------------------------------------------------------------------------- #

def detect_acpx_path(configured: str = "") -> str:
    """探测 acpx CLI 的可执行路径。

    优先级：显式配置 > bundled OpenClaw > ``which acpx`` > ``npx acpx``。
    """
    if configured and Path(configured).exists():
        return configured

    # 1. OpenClaw bundled 版本（glob 匹配版本哈希目录）。
    for pattern in (
        os.path.expanduser(
            "~/.openclaw/npm/projects/"
            "*/node_modules/@openclaw/acpx/node_modules/.bin/acpx"
        ),
    ):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    # 2. 系统 PATH 中的 acpx。
    which = shutil.which("acpx")
    if which:
        return which

    # 3. npx fallback（会在首次调用时自动安装）。
    return "npx acpx"


# --------------------------------------------------------------------------- #
#  结果类型
# --------------------------------------------------------------------------- #

@dataclass
class ACPResult:
    """一次 ACP 调用的结果。"""

    success: bool
    output: str = ""
    error: str = ""
    returncode: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    work_dir: Path | None = None  # CC 写入代码的目录


# --------------------------------------------------------------------------- #
#  ClaudeCodeACP
# --------------------------------------------------------------------------- #

class ClaudeCodeACP:
    """通过 acpx CLI 调用 Claude Code 的适配器。

    Claude Code 是**无状态的代码生成/修复服务**——每次调用给"当前需求 + 报错/结果"，
    返回修复后的代码。HAA 掌控循环（DebugSession），Claude Code 只管写代码。

    Parameters
    ----------
    campaign_id:
        用于构造唯一 session 名（``{prefix}-{campaign_id}``）。
    work_dir:
        Claude Code 的工作目录（cwd）。CC 在此目录内读写文件。创建和恢复会话
        必须在同一 cwd 下（acpx 按 cwd 检索会话索引）。
    config:
        :class:`~haa.config.ACPConfig`。
    """

    def __init__(
        self,
        campaign_id: str,
        work_dir: str | Path,
        config: ACPConfig | None = None,
    ) -> None:
        self.config = config or ACPConfig()
        self.campaign_id = campaign_id
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.session_name = f"{self.config.session_prefix}-{campaign_id}"
        self.acpx_path = detect_acpx_path(self.config.acpx_path)
        self._session_ensured = False

    # ------------------------------------------------------------------ #
    #  会话管理
    # ------------------------------------------------------------------ #

    def _ensure_session(self) -> None:
        """确保命名会话存在，不存在则创建。幂等。"""
        if self._session_ensured:
            return
        # Check if session already exists.
        check = self._run_acpx(
            ["claude", "sessions", "show", self.session_name],
            timeout=15,
            check=False,
        )
        if check.returncode != 0:
            logger.info("ACP: creating session '%s' in %s", self.session_name, self.work_dir)
            self._run_acpx(
                ["claude", "sessions", "new", "--name", self.session_name],
                timeout=30,
                check=True,
            )
        else:
            logger.info("ACP: reusing session '%s'", self.session_name)
        self._session_ensured = True

    def close(self) -> None:
        """关闭会话，释放资源。"""
        if not self._session_ensured:
            return
        self._run_acpx(
            ["claude", "sessions", "close", self.session_name],
            timeout=10,
            check=False,
        )
        self._session_ensured = False

    # ------------------------------------------------------------------ #
    #  底层 prompt 发送
    # ------------------------------------------------------------------ #

    def _run_acpx(
        self,
        args: list[str],
        *,
        timeout: int = 600,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """执行一条 acpx 命令（在 self.work_dir 下）。"""
        cmd = self._build_cmd(args)
        logger.debug("ACP exec: %s (cwd=%s, timeout=%d)", cmd, self.work_dir, timeout)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(self.work_dir),
            check=False,  # we handle returncode ourselves
        )

    def _build_cmd(self, args: list[str]) -> list[str]:
        """Build the full command list from acpx_path + args.

        Handles the ``npx acpx`` fallback (two tokens) vs a direct path (one token).
        """
        parts = self.acpx_path.split()
        return parts + args

    def send_prompt(self, prompt: str, *, timeout: int = 600) -> ACPResult:
        """向会话发送 prompt，返回 Claude Code 的输出。

        长 prompt 会自动写入临时文件用 ``-f`` 发送（避免 shell 转义）。
        """
        self._ensure_session()
        return self._send(prompt, timeout=timeout)

    def send_prompt_file(self, prompt_file: str | Path, *, timeout: int = 600) -> ACPResult:
        """从文件读取 prompt 发送（适合长 prompt / 结构化 prompt）。"""
        self._ensure_session()
        return self._send(str(prompt_file), from_file=True, timeout=timeout)

    def _send(
        self,
        prompt_or_file: str,
        *,
        from_file: bool = False,
        timeout: int = 600,
    ) -> ACPResult:
        """Internal: send a prompt to the session."""
        import time

        start = time.time()
        args = ["claude", "-s", self.session_name]
        if from_file:
            args += ["-f", prompt_or_file]
        else:
            # Write short prompts directly; long ones go through a temp file.
            if len(prompt_or_file) > 500:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", delete=False,
                    dir=str(self.work_dir), prefix="acp_prompt_",
                )
                tmp.write(prompt_or_file)
                tmp.close()
                args += ["-f", tmp.name]
            else:
                args += [prompt_or_file]

        try:
            proc = self._run_acpx(args, timeout=timeout, check=False)
            result = ACPResult(
                success=proc.returncode == 0,
                output=proc.stdout,
                error=proc.stderr,
                returncode=proc.returncode,
                duration_s=time.time() - start,
            )
            if not result.success:
                logger.warning(
                    "ACP prompt failed (rc=%d): %s",
                    proc.returncode, (proc.stderr or proc.stdout)[:300],
                )
            return result
        except subprocess.TimeoutExpired as exc:
            logger.warning("ACP prompt timed out after %ds: %s", timeout, exc)
            return ACPResult(
                success=False,
                error=f"timed out after {timeout}s",
                timed_out=True,
                duration_s=time.time() - start,
            )

    # ------------------------------------------------------------------ #
    #  高级接口（P2 stage 调用）
    # ------------------------------------------------------------------ #

    def generate_code(
        self,
        paper_precursor: dict[str, Any],
        exp_spec: dict[str, Any],
        *,
        timeout: int | None = None,
    ) -> ACPResult:
        """CODE_GEN：读论文前体 + EXP_SPEC → 生成完整 Python 项目。

        要求 Claude Code 把代码写入 ``self.work_dir``。
        """
        prompt = _format_codegen_prompt(paper_precursor, exp_spec, self.work_dir)
        result = self.send_prompt(prompt, timeout=timeout or self.config.timeout_module)
        result.work_dir = self.work_dir
        return result

    def fix_traceback(
        self,
        traceback_text: str,
        *,
        timeout: int | None = None,
    ) -> ACPResult:
        """Phase A debug：收 traceback → 返回修复。

        Claude Code 在同一 session 中记得之前的代码上下文，只需告诉它 traceback。
        """
        prompt = (
            "The experiment code crashed with the following traceback. "
            "Fix the code in the current workspace.\n\n"
            f"```\n{traceback_text}\n```\n\n"
            "Fix the root cause and ensure the code runs without errors. "
            "Do NOT silence or catch-and-ignore the error."
        )
        result = self.send_prompt(prompt, timeout=timeout or self.config.timeout_complex)
        result.work_dir = self.work_dir
        return result

    def diagnose_metrics(
        self,
        metrics: dict[str, Any],
        log_tail: str,
        *,
        timeout: int | None = None,
    ) -> ACPResult:
        """Phase B debug：收异常指标 → 诊断 + 修代码。

        代码能跑但结果不对（loss 发散 / 精度差 / 结果与理论矛盾）。
        """
        import json as _json

        prompt = (
            "The experiment code runs without crashing, but the results are anomalous. "
            "Diagnose the likely cause and fix the code.\n\n"
            f"## Metrics\n```json\n{_json.dumps(metrics, indent=2, default=str)}\n```\n\n"
            f"## Log tail\n```\n{log_tail[-2000:]}\n```\n\n"
            "Common causes to check: loss divergence, wrong learning rate, "
            "data preprocessing bugs, gradient explosion/vanishing, "
            "incorrect metric computation. Fix the most likely root cause."
        )
        result = self.send_prompt(prompt, timeout=timeout or self.config.timeout_complex)
        result.work_dir = self.work_dir
        return result

    # ------------------------------------------------------------------ #
    #  Context manager
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "ClaudeCodeACP":
        self._ensure_session()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
#  Prompt 格式化
# --------------------------------------------------------------------------- #

def _format_codegen_prompt(
    paper_precursor: dict[str, Any],
    exp_spec: dict[str, Any],
    work_dir: Path,
) -> str:
    """格式化 CODE_GEN prompt。

    遵循 ACP 迁移文档 §5.4 的建议：明确要求 CC 把最终代码写入指定目录。
    """
    import json as _json

    paper_title = paper_precursor.get("candidate_title", "(unknown)")
    paper_sections = paper_precursor.get("paper", {})
    abstract = paper_sections.get("abstract", "(not available)")
    method = paper_sections.get("method", "(not available)")

    exp_spec_json = _json.dumps(exp_spec, indent=2, default=str, ensure_ascii=False)

    return f"""You are tasked with generating a complete, runnable Python experiment project for a research paper.

## Paper: {paper_title}

### Abstract
{abstract}

### Method
{method}

## Experiment Specification
```json
{exp_spec_json}
```

## Requirements

Generate a COMPLETE Python project in the current working directory ({work_dir}). The project MUST include:

1. `data.py` — data loading / preprocessing (download if needed, or use synthetic data as fallback)
2. `model.py` — the core model / algorithm implementation
3. `train.py` — training loop (with logging, checkpointing optional)
4. `eval.py` — evaluation metrics computation
5. `requirements.txt` — all pip dependencies
6. `run.sh` — shell entry point: `python train.py && python eval.py`
7. `README.md` — brief description + how to run

## Critical Rules

- The code must be **directly runnable** via `bash run.sh` with no manual intervention.
- Use standard Python 3.10+ syntax.
- Write results to a `results/` directory (metrics as `results/metrics.json`, plots as `results/*.png`).
- Handle common failure modes gracefully (missing data → synthetic fallback, GPU unavailable → CPU).
- Keep training time reasonable for a single-GPU machine (< 1 hour for a small-scale validation).

Write all files now. After writing, verify the project structure is complete.
"""
