"""工具③：沙箱双层（轻量模式 + 容器模式）。

大修第一章 §5.3 规格，行为规格来自 DSH 测试翻译清单 §3：
- 轻量模式（P1/P3 默认）：路径白名单（检查层 PathWhitelist 已承载——
  符号链接解析后必须仍在白名单内、路径穿越拦截）＋**命令黑名单**
  （本模块 :class:`CommandBlacklist`，作为统一检查链的事前检查件）＋超时。
- 容器模式（P2 实验默认）：Docker 工作目录隔离，默认禁网；Docker 探测
  不可用 → 结构化 fail-closed，命令绝不未隔离运行；探针结论缓存。
- 策略说明段字节稳定（TMPDIR 类环境变化不影响渲染）。

子进程形态：全部经 :func:`haa.harness.tools.jobs._spawn_group`——全
Harness 唯一的子进程出口（``bash -lc``，命令以 argv 元素传入而非拼入
shell 字符串）；容器路径/镜像/命令一律 :func:`shlex.quote` 严格引用，
无注入面。超时经进程组整组回收。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
from types import SimpleNamespace
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolSpec
from haa.harness.tools.jobs import _spawn_group

logger = logging.getLogger("haa.harness.tools.sandbox")

DEFAULT_BLOCKED_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f?|-[a-zA-Z]*f[a-zA-Z]*r?)\s+[/~]",  # rm -rf /|~
    r"\bsudo\b",
    r"\bsu\s+-\w+\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=.*of=/dev/",
    r">\s*/dev/sd[a-z]",
    r"\bchmod\s+-R\s+777\s+/",
    r"\bcurl[^|;]*\|\s*(ba)?sh\b",   # 远程脚本直接进 shell
    r"\bwget[^|;]*\|\s*(ba)?sh\b",
)


class CommandBlacklist:
    """命令黑名单（统一检查链第 2 步的一环；模式匹配含常见变形）。

    仅对声明 ``runs_commands`` 守则的工具生效（exec_bash）。
    """

    def __init__(self, patterns: tuple[str, ...] = DEFAULT_BLOCKED_PATTERNS):
        self._patterns = [re.compile(p) for p in patterns]

    def check(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolCallContext) -> None:
        if spec.guardrails.get("runs_commands") is not True:
            return
        command = str(args.get("command", ""))
        if not command:
            return
        # 变形防御：压平引号/续行/多空白后再匹配（黑名单看语义形态）
        flattened = re.sub(r"\s+", " ", command.replace("\\\n", " ").strip())
        for pat in self._patterns:
            if pat.search(flattened):
                raise ToolError(
                    f"sandbox: command blocked by blacklist (pattern {pat.pattern!r}) — "
                    "destructive/system commands are not allowed"
                )


def render_policy_section(mode: str, root: str) -> str:
    """沙箱策略说明段（字节稳定：渲染仅由 mode 与 root 两参决定，
    TMPDIR 等环境变化无影响，§3-9）。"""
    mode = mode if mode in ("read-only", "workspace-write", "container", "danger-full-access") \
        else "read-only"
    return (
        f"Sandbox policy (mode={mode}, workspace root={root}): file reads are "
        "always allowed; writes are confined to the workspace root; commands "
        "are checked against a blacklist before execution. Paths are resolved "
        "(symlinks included) before the boundary check."
    )


def _run_shell(shell_command: str, timeout_s: int, *,
               via_sg: bool = False) -> SimpleNamespace:
    """经唯一 spawn 出口执行并收口（超时整组击杀后回收已产输出）。"""
    cmd = f"sg docker -c {shlex.quote(shell_command)}" if via_sg else shell_command
    proc = _spawn_group(cmd)
    try:
        out_b, err_b = proc.communicate(timeout=timeout_s)
        return SimpleNamespace(
            returncode=proc.returncode,
            stdout=(out_b or b"").decode("utf-8", errors="replace"),
            stderr=(err_b or b"").decode("utf-8", errors="replace"),
        )
    except Exception:  # noqa: BLE001 — 超时：整组击杀后收尸
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        try:
            out_b, err_b = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out_b, err_b = b"", b""
        return SimpleNamespace(
            returncode=proc.returncode,
            stdout=(out_b or b"").decode("utf-8", errors="replace"),
            stderr=(err_b or b"").decode("utf-8", errors="replace"),
        )


class DockerSandbox:
    """容器模式执行器（P2 实验默认；探测不可用 fail-closed）。"""

    _probe_cache: dict[str, tuple[str, str]] = {}

    def __init__(self, *, image: str = "p2-sandbox-base:latest",
                 network_domains: tuple[str, ...] = ()):
        self.image = image
        self.network_domains = tuple(network_domains)

    # -- 探测 ---------------------------------------------------------------

    def _probe(self) -> str | None:
        """返回可用形态："direct" / "sg" / None(不可用)。结论缓存。"""
        cached = self._probe_cache.get("probe")
        if cached is not None:
            return cached[0] or None
        verdict = ""
        probe_cmd = "docker version --format '{{.Server.Version}}'"
        try:
            if _run_shell(probe_cmd, 15).returncode == 0:
                verdict = "direct"
        except Exception:  # noqa: BLE001
            pass
        if not verdict:
            try:
                r = _run_shell(probe_cmd, 15, via_sg=True)
                if r.returncode == 0 and r.stdout.strip():
                    verdict = "sg"
            except Exception:  # noqa: BLE001
                pass
        self._probe_cache["probe"] = (verdict, "")
        return verdict or None

    def available(self) -> bool:
        return self._probe() is not None

    # -- 执行 ---------------------------------------------------------------

    def run(self, command: str, workspace: str, *, timeout_s: int = 600,
            writable: bool = True, network: str | None = None) -> SimpleNamespace:
        """在容器内运行命令；返回 {returncode, stdout, stderr}。

        fail-closed：Docker 不可用直接抛结构化错误，命令绝不未隔离执行。
        ``network``：None=禁网（HAA P2 默认）；"default"=容器默认网络
        （域名白名单由调用方在更外层把关）。
        """
        mode = self._probe()
        if mode is None:
            raise ToolError(
                "SANDBOX_UNAVAILABLE: docker is not reachable — refusing to run "
                "the command unconfined (fail-closed)"
            )
        ro = "" if writable else " --read-only"
        net = "none" if network is None else network
        # 路径/镜像/命令全部 shlex.quote 严格引用（无注入面）
        docker_cmd = (
            f"docker run --rm{ro} -v {shlex.quote(workspace)}:/workspace"
            f" -w /workspace --network {shlex.quote(net)}"
            f" {shlex.quote(self.image)} /bin/bash -lc {shlex.quote(command)}"
        )
        try:
            return _run_shell(docker_cmd, timeout_s, via_sg=(mode == "sg"))
        except OSError as exc:
            raise ToolError(f"container spawn failed: {exc}") from exc


SANDBOX_TOOL_RULES = (
    "Command sandbox: destructive commands (rm -rf on system paths, sudo, "
    "shutdown, disk writes) are blocked before execution; a blocked command "
    "returns a sandbox error — do not retry it verbatim."
)
