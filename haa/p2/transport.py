"""P2 执行传输层——DebugSession 的执行后端抽象。

提供两种实现：
- :class:`LocalDebugTransport`：本地 subprocess 直接执行（测试/开发用）。
- :class:`SSHDebugTransport`：基于 :class:`haa.remote.transport.SSHTransport`（生产用）。

DebugSession 通过统一的 :class:`ExecutionTransport` 接口调用，不关心后端。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("haa.p2.transport")


# --------------------------------------------------------------------------- #
#  结果类型
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    """一次代码执行的输出。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    results_dir: Path | None = None  # 下载到本地的 results/ 目录

    @property
    def crashed(self) -> bool:
        """True 如果代码崩溃了（非零退出码 = 硬错误）。"""
        return self.exit_code != 0

    @property
    def combined_log(self) -> str:
        """stdout + stderr 合并（供 traceback 诊断）。"""
        return (self.stdout or "") + "\n" + (self.stderr or "")


# --------------------------------------------------------------------------- #
#  Protocol
# --------------------------------------------------------------------------- #

@runtime_checkable
class ExecutionTransport(Protocol):
    """DebugSession 的执行后端抽象。"""

    def deploy(self, code_dir: str | Path, run_id: str) -> str:
        """部署代码到执行环境，返回执行目录标识（remote_dir 或 local_dir）。

        ``run_id`` 用于隔离不同轮次的执行（debug round）。
        """
        ...

    def run(
        self, exec_dir: str, entry_command: str, *, timeout: int | None = None
    ) -> RunResult:
        """在执行环境中运行命令，返回结果。"""
        ...

    def download_results(self, exec_dir: str, local_dir: str | Path) -> Path:
        """下载 results/ 目录到本地，返回本地路径。"""
        ...


# --------------------------------------------------------------------------- #
#  LocalDebugTransport
# --------------------------------------------------------------------------- #

class LocalDebugTransport:
    """本地执行后端——subprocess 直接跑，不 SSH。

    用于测试/开发：DebugSession 的逻辑验证不依赖远程服务器。
    ``base_dir`` 是本地工作根目录，每个 run_id 创建一个子目录。
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir()) / "haa_local_exec"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def deploy(self, code_dir: str | Path, run_id: str) -> str:
        """复制代码到 ``base_dir/run_id/`` 并返回该路径。"""
        dest = self.base_dir / run_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(str(code_dir), str(dest))
        logger.debug("LocalTransport: deployed %s → %s", code_dir, dest)
        return str(dest)

    def run(
        self, exec_dir: str, entry_command: str, *, timeout: int | None = None
    ) -> RunResult:
        """在 exec_dir 中执行命令（subprocess）。"""
        start = time.time()
        try:
            proc = subprocess.run(
                entry_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=exec_dir,
            )
            return RunResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=time.time() - start,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                exit_code=-1,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") + f"\n[TIMEOUT after {timeout}s]",
                timed_out=True,
                duration_s=time.time() - start,
            )

    def download_results(self, exec_dir: str, local_dir: str | Path) -> Path:
        """本地模式下 results 已在 exec_dir/results/，直接复制。"""
        src = Path(exec_dir) / "results"
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if src.exists():
            for item in src.iterdir():
                target = dest / item.name
                if target.exists():
                    target.unlink()
                shutil.copy2(str(item), str(target))
        return dest


# --------------------------------------------------------------------------- #
#  SSHDebugTransport
# --------------------------------------------------------------------------- #

class SSHDebugTransport:
    """SSH 远程执行后端——基于 :class:`haa.remote.transport.SSHTransport`。

    生产用：DebugSession 通过 SSH 上传代码 → 远程执行 → 下载结果。
    复用 SSHTransport 的 ControlMaster 连接复用。
    """

    def __init__(self, ssh_config: Any) -> None:
        from haa.remote.transport import SSHTransport  # lazy import to avoid dep at module load

        self.config = ssh_config
        self.transport = SSHTransport(ssh_config)

    def deploy(self, code_dir: str | Path, run_id: str) -> str:
        """打包代码 → 上传 → 解压到远程目录。返回 remote_dir。"""
        import tarfile  # lazy: only needed for SSH deploy

        remote_dir = f"{self.config.remote_base_dir}/{run_id}"
        self.transport.run(f"mkdir -p {remote_dir}")

        # Pack code.
        pkg = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        pkg.close()
        with tarfile.open(pkg.name, "w:gz") as tar:
            for item in Path(code_dir).iterdir():
                if item.name in ("__pycache__", ".git", ".venv", "node_modules"):
                    continue
                tar.add(str(item), arcname=item.name)

        remote_pkg = f"{remote_dir}/code.tar.gz"
        self.transport.upload(pkg.name, remote_pkg)
        self.transport.run(
            f"cd {remote_dir} && tar xzf code.tar.gz && "
            f"pip install -r requirements.txt 2>>install.log || true",
            timeout=300,
        )
        logger.info("SSHTransport: deployed to %s", remote_dir)
        return remote_dir

    def run(
        self, exec_dir: str, entry_command: str, *, timeout: int | None = None
    ) -> RunResult:
        """在远程目录执行命令（前台阻塞，等待完成）。"""
        start = time.time()
        full_cmd = f"cd {exec_dir} && {entry_command} 2>&1"
        try:
            stdout = self.transport.run(full_cmd, timeout=timeout or 3600)
            # SSHTransport.run raises SSHError on non-zero exit; if we get here, exit_code=0.
            return RunResult(
                exit_code=0,
                stdout=stdout,
                duration_s=time.time() - start,
            )
        except Exception as exc:
            # Could be SSHError (non-zero exit) or timeout.
            from haa.remote.transport import SSHError

            if isinstance(exc, SSHError):
                # Try to fetch the log for diagnosis.
                try:
                    log = self.transport.run(f"cd {exec_dir} && cat run.log 2>/dev/null || true")
                except Exception:
                    log = str(exc)
                return RunResult(
                    exit_code=1,
                    stdout=log,
                    stderr=str(exc),
                    duration_s=time.time() - start,
                )
            raise

    def download_results(self, exec_dir: str, local_dir: str | Path) -> Path:
        """下载远程 results/ 目录 + run.log 到本地。"""
        from haa.remote.transport import SSHError

        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for remote_file, local_name in [
            (f"{exec_dir}/run.log", "run.log"),
            (f"{exec_dir}/results/", "results/"),
        ]:
            try:
                self.transport.download(remote_file, str(dest / local_name))
            except SSHError as exc:
                logger.warning("partial download failed (%s): %s", remote_file, exc)
        return dest
