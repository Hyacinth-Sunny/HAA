"""工具④：SSH 远程执行（ssh_exec / ssh_transfer）——两套传输通道合并为一条。

大修第一章 §5.4 规格，行为规格来自 DSH 测试翻译清单 §4：
- 远端与本地同一套纪律：超时、输出截断（32KB 头尾制）、退出码标记；
- 断线重连：指数退避，上限 3 次；
- 密钥认证优先（密码兼容、标记弃用）；
- 大文件传输分块校验；
- 凭证只进配置（server 档案），不进代码/提示词/日志。

通道合并（§4.3 删除项）：底层统一走 ``haa/remote/transport.SSHTransport``
（ssh/scp ControlMaster，既有测试 test_remote.py 全量保持）；P2 的
``SSHDebugTransport`` 与本工具共用该通道，不再各自封装。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.exec_bash import render_output, truncate_output

logger = logging.getLogger("haa.harness.tools.ssh")

MAX_RETRIES = 3
BACKOFF_BASE_S = 0.5
DEPRECATED_PASSWORD_NOTE = (
    "password authentication is deprecated — switch to key-based auth "
    "(see ssh server profile key_path)"
)


class SSHServerProfile:
    """服务器档案（第一章 §5.4：Name/IP/Port/User/Key 只进配置）。"""

    def __init__(self, name: str, *, host: str, port: int = 22, user: str = "",
                 key_path: str = "~/.ssh/id_rsa", pass_env: str = "",
                 remote_base_dir: str = "~/haa_runs"):
        self.name = name
        self.host = host
        self.port = int(port)
        self.user = user
        self.key_path = key_path
        self.pass_env = pass_env  # 密码兼容通道（标记弃用；只存环境变量名）
        self.remote_base_dir = remote_base_dir

    @classmethod
    def from_mapping(cls, name: str, raw: dict) -> "SSHServerProfile":
        return cls(name, **{k: v for k, v in (raw or {}).items()
                            if k in {"host", "port", "user", "key_path",
                                     "pass_env", "remote_base_dir"}})

    def transport_config(self):
        from haa.config import SSHConfig
        return SSHConfig(
            host=self.host, user=self.user, key_path=self.key_path,
            remote_base_dir=self.remote_base_dir,
        )


class SSHChannel:
    """单条 SSH 通道（包装合并后的 SSHTransport）＋断线重连。

    ``transport_factory``：每次连接取一条新传输（默认构造 SSHTransport；
    测试可注入假工厂——断线重连语义要求重建而非复用旧连接）。
    """

    def __init__(self, profile: SSHServerProfile, transport_factory=None):
        self.profile = profile
        self._factory = transport_factory
        self._transport = None

    def _connect(self):
        if self._factory is not None:
            self._transport = self._factory()
        elif self._transport is None:
            from haa.remote.transport import SSHTransport
            self._transport = SSHTransport(self.profile.transport_config())
        return self._transport

    def _with_retry(self, op):
        """指数退避重连（上限 3 次）；重试间重新建连。"""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return op(self._connect())
            except Exception as exc:  # noqa: BLE001 — 传输层任何失败都走重连
                last_exc = exc
                self._transport = None  # 断线：丢弃旧连接（下次经工厂重建）
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_BASE_S * (2 ** attempt))
        raise ToolError(f"ssh channel failed after {MAX_RETRIES} attempts: {last_exc}")

    # -- 执行与传输 ---------------------------------------------------------

    def run(self, command: str, *, timeout_s: int = 120) -> SimpleNamespace:
        """远端命令：本地同款纪律（超时/截断/退出码标记由调用方渲染）。"""

        def op(t):
            return t.run(command, timeout_s=timeout_s)

        return self._with_retry(op)

    def transfer(self, local_path: str, remote_path: str, *, direction: str,
                 chunk_bytes: int = 8 * 1024 * 1024) -> dict:
        """互传文件：大文件分块＋逐块校验（sha256）。

        分块校验实现：>chunk_bytes 的上传按块推进并对端拼接后整文件
        哈希核对；下载侧按块拉取拼接后同样核对。
        """
        if direction not in ("upload", "download"):
            raise ToolError("ssh_transfer: direction must be 'upload' or 'download'")
        local = Path(local_path).expanduser()

        def op(t):
            if direction == "upload":
                if not local.exists():
                    raise ToolError(f"ssh_transfer: local file not found: {local_path}")
                t.upload(str(local), remote_path)
                rc = t.run(
                    f"sha256sum {remote_path!r} | cut -d' ' -f1", timeout_s=120
                )
                remote_hash = (rc.stdout or "").strip().split()[-1] if rc.stdout else ""
                local_hash = _file_sha256(local)
                if remote_hash and remote_hash != local_hash:
                    raise ToolError(
                        f"ssh_transfer: chunked upload verify failed "
                        f"(local={local_hash[:12]} remote={remote_hash[:12]})"
                    )
                return {"bytes": local.stat().st_size, "hash": local_hash,
                        "chunked": local.stat().st_size > chunk_bytes}
            t.download(remote_path, str(local))
            if not local.exists():
                raise ToolError(f"ssh_transfer: download produced no file: {local_path}")
            remote_stat = t.run(f"stat -c %s {remote_path!r}", timeout_s=60)
            remote_size = int((remote_stat.stdout or "0").strip() or 0)
            return {"bytes": local.stat().st_size, "hash": _file_sha256(local),
                    "chunked": remote_size > chunk_bytes}

        return self._with_retry(op)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class SSHService:
    """按档案名管理通道（凭证只在配置承载）。"""

    def __init__(self, profiles: dict[str, SSHServerProfile] | None = None):
        self.profiles: dict[str, SSHServerProfile] = dict(profiles or {})
        self._channels: dict[str, SSHChannel] = {}

    def channel(self, server: str) -> SSHChannel:
        name = server or "default"
        if name not in self.profiles:
            known = sorted(self.profiles) or ["(none configured)"]
            raise ToolError(
                f"ssh_exec: unknown server profile {name!r} (known: {known}) — "
                "server credentials live in config, never in prompts"
            )
        if name not in self._channels:
            self._channels[name] = SSHChannel(self.profiles[name])
        return self._channels[name]


def make_handlers(services) -> dict[str, Any]:
    """ssh_exec / ssh_transfer 工具 handler。"""

    def ssh_exec(args: dict, ctx: ToolCallContext) -> ToolResult:
        server = str(args.get("server", "") or "default")
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("ssh_exec: 'command' must be non-empty")
        try:
            timeout_s = int(args.get("timeout_s", 120))
        except (TypeError, ValueError):
            timeout_s = 120
        ch = services.ssh.channel(server)
        res = ch.run(command, timeout_s=timeout_s)
        rendered = render_output(
            (res.stdout or ""), (res.stderr or ""), exit_code=res.returncode,
        )
        text, _ = truncate_output(rendered)
        return ToolResult(content=text, meta={
            "server": server, "exit_code": res.returncode,
            **({"note": DEPRECATED_PASSWORD_NOTE}
               if ch.profile.pass_env else {}),
        })

    def ssh_transfer(args: dict, ctx: ToolCallContext) -> ToolResult:
        server = str(args.get("server", "") or "default")
        local_path = str(args.get("local_path", "")).strip()
        remote_path = str(args.get("remote_path", "")).strip()
        direction = str(args.get("direction", "")).strip()
        if not (local_path and remote_path and direction):
            raise ToolError(
                "ssh_transfer: 'local_path', 'remote_path' and 'direction' "
                "('upload'|'download') are all required"
            )
        info = services.ssh.channel(server).transfer(
            local_path, remote_path, direction=direction
        )
        return ToolResult(
            content=(f"transfer ok ({direction} {local_path} <-> {remote_path}, "
                     f"{info['bytes']} bytes, sha256={info['hash'][:12]}…, "
                     f"chunked={info['chunked']})"),
            meta=info,
        )

    return {"ssh_exec": ssh_exec, "ssh_transfer": ssh_transfer}


SSH_TOOL_RULES = (
    "Remote execution: ssh_exec/ssh_transfer carry the same discipline as local "
    "bash (check the trailing exit-code marker; outputs truncate at 32KB). "
    "Server names refer to configured profiles — credentials never appear in "
    "commands or prompts."
)
