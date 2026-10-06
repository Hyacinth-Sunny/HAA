"""远程实验执行器——编排 SSH 传输 + 代码部署 + 执行 + 结果收集。

Part II 的入口，在 Part I pipeline 中作为独立模块提供，供后续 CODE_GEN /
EXECUTE / ANALYZE stage 调用。v0.x 先提供基础设施，stage 集成在后续 phase。
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from haa.config import SSHConfig
from haa.remote.transport import SSHError, SSHTransport

logger = logging.getLogger("haa.remote.executor")


@dataclass
class ExecutionResult:
    """一次远程实验执行的结果。"""

    success: bool
    remote_dir: str
    pid: str = ""
    exit_code: int | None = None
    log: str = ""
    results_path: str = ""
    error: str = ""
    duration_s: float = 0.0


class RemoteExecutor:
    """编排远程实验的完整生命周期。"""

    def __init__(self, config: SSHConfig):
        self.config = config
        self.transport = SSHTransport(config)

    def deploy_and_run(
        self,
        campaign_id: str,
        code_dir: str,
        entry_command: str = "python main.py",
        timeout_s: int | None = None,
    ) -> ExecutionResult:
        """完整流程：打包 → 上传 → 执行 → 轮询 → 下载结果。"""
        start = time.time()
        remote_dir = f"{self.config.remote_base_dir}/{campaign_id}"

        try:
            self.transport.run(f"mkdir -p {remote_dir}")
            pkg_path = self._pack_code(code_dir, campaign_id)
            remote_pkg = f"{remote_dir}/code.tar.gz"
            self.transport.upload(pkg_path, remote_pkg)

            setup_cmd = (
                f"cd {remote_dir} && tar xzf code.tar.gz && "
                f"pip install -r requirements.txt 2>>install.log || true"
            )
            self.transport.run(setup_cmd, timeout=300)

            run_cmd = f"cd {remote_dir} && {entry_command} > run.log 2>&1"
            pid = self.transport.run_background(run_cmd)
            logger.info("远程实验已启动: pid=%s, dir=%s", pid, remote_dir)

            result = self.transport.poll_process(pid, remote_dir, timeout_s)

            local_results = tempfile.mkdtemp(prefix=f"haa_results_{campaign_id}_")
            try:
                self.transport.download(f"{remote_dir}/run.log", f"{local_results}/run.log")
                self.transport.download(f"{remote_dir}/results/", local_results + "/results/")
            except SSHError as exc:
                logger.warning("部分结果下载失败: %s", exc)

            return ExecutionResult(
                success=True, remote_dir=remote_dir, pid=pid,
                log=result.get("log_tail", ""), results_path=local_results,
                duration_s=time.time() - start,
            )
        except SSHError as exc:
            logger.error("远程执行失败: %s", exc)
            return ExecutionResult(
                success=False, remote_dir=remote_dir, error=str(exc),
                duration_s=time.time() - start,
            )

    @staticmethod
    def _pack_code(code_dir: str, campaign_id: str) -> str:
        """将代码目录打包为 tar.gz（排除 __pycache__/.git/.venv 等）。"""
        pkg_path = f"/tmp/haa_code_{campaign_id}.tar.gz"
        with tarfile.open(pkg_path, "w:gz") as tar:
            for item in Path(code_dir).iterdir():
                if item.name in ("__pycache__", ".git", ".venv", "node_modules"):
                    continue
                tar.add(str(item), arcname=item.name)
        return pkg_path
