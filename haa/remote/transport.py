"""SSH 传输层——参考 HM-Pro 的 ``control_plane/transport.py``。

使用 SSH ControlMaster 复用连接，避免每次操作都建立新连接。
所有操作通过 subprocess 调 ssh/scp，不依赖 paramiko（减少依赖）。
SSHConfig 定义在 :mod:`haa.config`（frozen dataclass，配置层统一）。
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from typing import Any

from haa.config import SSHConfig

logger = logging.getLogger("haa.remote")


class SSHError(RuntimeError):
    """SSH 操作失败。"""


class SSHTransport:
    """SSH 传输层，复用 HM-Pro 的 ControlPath 模式。"""

    def __init__(self, config: SSHConfig):
        self.config = config
        self._host = f"{config.user}@{config.host}" if config.user else config.host
        key = os.path.expanduser(config.key_path)
        self._control_path = config.control_path or f"/tmp/haa_ssh_{config.host}"
        self._base_args = [
            "ssh",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=600",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-i", key,
            self._host,
        ]

    def _connect(self) -> None:
        """建立 ControlMaster 连接（如果尚未建立）。"""
        try:
            subprocess.run(
                self._base_args + ["true"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SSHError(f"SSH 连接失败: {exc}") from exc

    def run(self, command: str, timeout: int | None = None) -> str:
        """在远程执行命令，返回 stdout。"""
        self._connect()
        try:
            proc = subprocess.run(
                self._base_args + [command],
                capture_output=True, text=True,
                timeout=timeout or 60,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"SSH 命令超时: {exc}") from exc
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "remote command failed").strip()
            raise SSHError(f"SSH rc={proc.returncode}: {msg[:500]}")
        return proc.stdout

    def upload(self, local_path: str, remote_path: str) -> None:
        """上传文件到远程（scp）。"""
        key = os.path.expanduser(self.config.key_path)
        scp_args = [
            "scp", "-o", f"ControlPath={self._control_path}",
            "-o", "BatchMode=yes", "-i", key,
            local_path, f"{self._host}:{remote_path}",
        ]
        try:
            proc = subprocess.run(scp_args, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"SCP 上传超时: {exc}") from exc
        if proc.returncode != 0:
            raise SSHError(f"SCP 失败: {(proc.stderr or '').strip()[:500]}")

    def download(self, remote_path: str, local_path: str) -> None:
        """从远程下载文件（scp）。"""
        key = os.path.expanduser(self.config.key_path)
        scp_args = [
            "scp", "-o", f"ControlPath={self._control_path}",
            "-o", "BatchMode=yes", "-i", key,
            f"{self._host}:{remote_path}", local_path,
        ]
        try:
            proc = subprocess.run(scp_args, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"SCP 下载超时: {exc}") from exc
        if proc.returncode != 0:
            raise SSHError(f"SCP 失败: {(proc.stderr or '').strip()[:500]}")

    def run_background(self, command: str) -> str:
        """在远程以 nohup 后台启动命令，返回 PID。"""
        wrapped = f"nohup bash -c {shlex.quote(command)} > /dev/null 2>&1 & echo $!"
        output = self.run(wrapped)
        return output.strip()

    def poll_process(self, pid: str, remote_dir: str,
                     timeout: int | None = None) -> dict[str, Any]:
        """轮询远程进程是否完成。"""
        deadline = time.time() + (timeout or self.config.poll_timeout_s)
        while time.time() < deadline:
            try:
                self.run(f"kill -0 {pid} 2>/dev/null", timeout=10)
                time.sleep(self.config.poll_interval_s)
            except SSHError:
                break  # kill -0 失败 = 进程已结束
        try:
            log_tail = self.run(f"tail -100 {remote_dir}/run.log 2>/dev/null", timeout=30)
        except SSHError:
            log_tail = ""
        return {"pid": pid, "completed": True, "log_tail": log_tail}
