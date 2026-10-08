"""M1 原生工具接管——把五个底层工具的原生实现换进注册表。

接管语义（对照第一章 §4.1"替换（重写）"四件 + 新增件）：
- **同名替换**（模型可见契约不变，菜单不变）：exec_bash / read_file /
  write_file / edit_file——旧 handler 换成原生实现，参数 schema 保持
  兼容（legacy 字段全保留，新字段增量加入）；
- **新增注册**：job_output / job_kill / ssh_exec / ssh_transfer /
  experiment_status（菜单随后续批次铺进 P2 阶段；M1 期间 job 双件随
  exec_bash 同菜单）；
- **检查链增强**：命令黑名单（CommandBlacklist）挂入事前检查链；
- **楼层投稿**：五个工具的跨调用守则投稿楼层 100-199（M0 楼层化挂账
  清偿时由 BaseStage 统一装配）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from haa.harness.prompt_sections import FLOOR_TOOL_RULES, Section
from haa.harness.registry import ToolRegistry, ToolSpec
from haa.harness.tools import exec_bash as bash_mod
from haa.harness.tools import misc as misc_mod
from haa.harness.tools import run_experiment as runexp_mod
from haa.harness.tools import experiment_status as monitor_mod
from haa.harness.tools import fs_tools
from haa.harness.tools import sandbox as sandbox_mod
from haa.harness.tools import ssh_tools
from haa.harness.tools.exec_bash import ForegroundRunner
from haa.harness.tools.experiment_status import MonitorService
from haa.harness.tools.jobs import JobManager
from haa.harness.tools.sandbox import CommandBlacklist, DockerSandbox
from haa.harness.tools.ssh_tools import SSHServerProfile, SSHService

logger = logging.getLogger("haa.harness.tools.native")


class NativeServices:
    """原生工具共享的服务面（每注册表一份，与检查层同生命周期）。"""

    def __init__(self, *, allowed_roots: tuple[Path, ...] = (),
                 max_wait_s: float = 10.0,
                 ssh_profiles: dict[str, dict] | None = None,
                 max_concurrent_jobs: int = 10,
                 foreground: ForegroundRunner | None = None):
        self.allowed_roots = tuple(Path(r) for r in allowed_roots)
        self.max_wait_s = max_wait_s
        self.foreground = foreground or ForegroundRunner()
        self.jobs = JobManager(max_concurrent_per_owner=max_concurrent_jobs)
        self.ssh = SSHService({
            name: SSHServerProfile.from_mapping(name, raw)
            for name, raw in (ssh_profiles or {}).items()
        })
        self.monitor = MonitorService(self.jobs)
        self.sandbox = CommandBlacklist()
        self.docker = DockerSandbox()
        self.budget_ref = None  # 可注入的预算管理器引用（budget_status 用）

    def path_allowed(self, path: str) -> bool:
        p = Path(path).expanduser()
        resolved = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
        for root in self.allowed_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False


def _emit_via(registry: ToolRegistry):
    """job/state 事件经检查层会话日志发射（attach 后保持同本账）。"""

    def emit(job_id: str, state: str, detail: str) -> None:
        try:
            checklist = registry._ensure_checklist()
            checklist.session_log.job_state(
                job_id=job_id, state=state, detail=detail
            )
        except Exception:  # noqa: BLE001 — 事件发射不炸任务
            logger.exception("job/state emit failed: %s", job_id)

    return emit


# --------------------------------------------------------------------------- #
#  参数 schema（替换件保持 legacy 字段兼容，新字段增量）
# --------------------------------------------------------------------------- #

EXEC_BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The shell command to run."},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 120).",
                    "default": 120},
        "run_in_background": {"type": "boolean",
                              "description": "Run as a background job; returns a job_id "
                                             "readable via job_output (default false).",
                              "default": False},
        "cwd": {"type": "string", "description": "Working directory (must be inside the "
                                                 "campaign sandbox)."},
    },
    "required": ["command"],
}

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path (relative to the campaign "
                                                  "dir, or absolute within it)."},
        "max_chars": {"type": "integer", "description": "Output size cap in characters.",
                      "default": 30000},
        "offset": {"type": "integer", "description": "1-based first line to show."},
        "limit": {"type": "integer", "description": "Max lines to show (<= 2000)."},
    },
    "required": ["path"],
}

WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path (relative to the campaign "
                                                  "dir, or absolute within it)."},
        "content": {"type": "string", "description": "The full text to write."},
    },
    "required": ["path", "content"],
}

EDIT_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path (relative to the campaign dir)."},
        "old_string": {"type": "string", "description": "The exact text to replace "
                                                        "(must be unique, or set replace_all)."},
        "new_string": {"type": "string", "description": "The replacement text."},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence. "
                                                          "Default false."},
    },
    "required": ["path", "old_string", "new_string"],
}

JOB_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "description": "Background job id from exec_bash."},
        "offset": {"type": "integer", "description": "Byte offset to read from "
                                                     "(omit to continue where you left off)."},
        "wait": {"type": "boolean", "description": "Block until the job settles "
                                                   "(bounded by server cap). Default false."},
        "wait_timeout_s": {"type": "number", "description": "Your wait budget in seconds "
                                                            "(clamped by server cap)."},
    },
    "required": ["job_id"],
}

JOB_KILL_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "description": "Background job id to terminate."},
        "reason": {"type": "string", "description": "Why the job is being killed."},
    },
    "required": ["job_id"],
}

SSH_EXEC_SCHEMA = {
    "type": "object",
    "properties": {
        "server": {"type": "string", "description": "Configured server profile name."},
        "command": {"type": "string", "description": "Remote shell command."},
        "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 120)."},
    },
    "required": ["command"],
}

SSH_TRANSFER_SCHEMA = {
    "type": "object",
    "properties": {
        "server": {"type": "string", "description": "Configured server profile name."},
        "local_path": {"type": "string", "description": "Local file path."},
        "remote_path": {"type": "string", "description": "Remote file path."},
        "direction": {"type": "string", "enum": ["upload", "download"],
                      "description": "Transfer direction."},
    },
    "required": ["local_path", "remote_path", "direction"],
}

EXPERIMENT_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "description": "Local job id or remote experiment id."},
        "log_tail": {"type": "boolean", "description": "Include the log tail (default true)."},
    },
    "required": ["job_id"],
}


def apply_native_tools(registry: ToolRegistry, *, allowed_roots: tuple[Path, ...] = (),
                       ssh_profiles: dict[str, dict] | None = None,
                       max_wait_s: float = 10.0,
                       max_concurrent_jobs: int = 10) -> NativeServices:
    """在既有注册表上完成原生接管（桥接先注册全部旧件，此处替换/新增）。"""
    services = NativeServices(
        allowed_roots=allowed_roots, ssh_profiles=ssh_profiles,
        max_wait_s=max_wait_s, max_concurrent_jobs=max_concurrent_jobs,
    )
    services.jobs._emit = _emit_via(registry)  # job/state 事件同本账
    # 事件闭包持有同一 JobManager 引用（emit 回调在 Job 构造时绑定，
    # 上面替换的是 manager 级回调——start 时传入）
    bash_handlers = bash_mod.make_handlers(services)
    fs_handlers = {
        "read_file": fs_tools.make_read_file(services),
        "write_file": fs_tools.make_write_file(services),
        "edit_file": fs_tools.make_edit_file(services),
    }
    ssh_handlers = ssh_tools.make_handlers(services)
    monitor_handlers = monitor_mod.make_handlers(services)

    replacements = [
        ("exec_bash", EXEC_BASH_SCHEMA, bash_handlers["exec_bash"],
         ("SCREEN", "VERIFY"), {"runs_commands": True}),
        ("read_file", READ_FILE_SCHEMA, fs_handlers["read_file"],
         "*", {"reads_paths": True}),
        ("write_file", WRITE_FILE_SCHEMA, fs_handlers["write_file"],
         ("SCREEN", "DESIGN", "WRITE", "REFINE", "EXP_SPEC", "PILOT"), {"writes_paths": True}),
        ("edit_file", EDIT_FILE_SCHEMA, fs_handlers["edit_file"],
         ("SCREEN", "DESIGN", "WRITE", "REFINE"), {"writes_paths": True}),
    ]
    additions = [
        ("job_output", JOB_OUTPUT_SCHEMA, bash_handlers["job_output"],
         ("SCREEN", "VERIFY"), {}),
        ("job_kill", JOB_KILL_SCHEMA, bash_handlers["job_kill"],
         ("SCREEN", "VERIFY"), {}),
        ("ssh_exec", SSH_EXEC_SCHEMA, ssh_handlers["ssh_exec"], (), {}),
        ("ssh_transfer", SSH_TRANSFER_SCHEMA, ssh_handlers["ssh_transfer"], (), {}),
        ("experiment_status", EXPERIMENT_STATUS_SCHEMA,
         monitor_handlers["experiment_status"], (), {}),
    ]
    for name, schema, handler, stages, guardrails in replacements:
        registry.unregister(name)
        registry.register(ToolSpec(
            name=name, description=f"[native M1] {name}",
            parameters=schema, stages=stages, handler=handler,
            guardrails=guardrails, source="harness.tools.native",
        ))
    misc_handlers = misc_mod.make_handlers(services)
    runexp_handlers = runexp_mod.make_handlers(services)
    additions += [
        ("glob", {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Filename pattern, e.g. '*.md' or 'exp_*.py'."},
                "max_results": {"type": "integer",
                                "description": "Cap on matches (<=500, default 200)."},
            },
            "required": ["pattern"],
        }, misc_handlers["glob"], "*", {}),
        ("budget_status", {
            "type": "object",
            "properties": {},
            "required": [],
        }, misc_handlers["budget_status"], (), {}),
        ("run_experiment", {
            "type": "object",
            "properties": {
                "workspace": {"type": "string",
                              "description": "Experiment dir containing solve.sh."},
                "timeout_s": {"type": "integer",
                              "description": "Timeout in seconds (default 600, max 7200)."},
                "container": {"type": "boolean",
                              "description": "Run in the docker sandbox (default true)."},
            },
            "required": ["workspace"],
        }, runexp_handlers["run_experiment"], (), {}),
    ]
    for name, schema, handler, stages, guardrails in additions:
        registry.unregister(name)
        registry.register(ToolSpec(
            name=name, description=f"[native M1] {name}",
            parameters=schema, stages=stages, handler=handler,
            guardrails=guardrails, source="harness.tools.native",
        ))
    # 检查链增强：命令黑名单（幂等——重复接管不重复挂）
    checklist = registry._ensure_checklist()
    if not any(isinstance(c, CommandBlacklist) for c in checklist.prechecks):
        checklist.prechecks.append(services.sandbox)
    registry.native = services
    return services


def native_prompt_sections() -> list[Section]:
    """五个工具的跨调用守则（楼层 100-199，工具自己投稿——§3.3）。"""
    return [
        Section(source="tool:exec_bash", floor=FLOOR_TOOL_RULES[0],
                text=bash_mod.EXIT_CODE_HABIT),
        Section(source="tool:fs", floor=FLOOR_TOOL_RULES[0] + 1,
                text=fs_tools.FS_TOOL_RULES),
        Section(source="tool:sandbox", floor=FLOOR_TOOL_RULES[0] + 2,
                text=sandbox_mod.SANDBOX_TOOL_RULES),
        Section(source="tool:ssh", floor=FLOOR_TOOL_RULES[0] + 3,
                text=ssh_tools.SSH_TOOL_RULES),
        Section(source="tool:monitor", floor=FLOOR_TOOL_RULES[0] + 4,
                text=monitor_mod.MONITOR_TOOL_RULES),
    ]
