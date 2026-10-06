"""远程实验执行模块（Phase B / Part II 入口）。

提供 SSH 传输层（SSHTransport）和远程实验编排（RemoteExecutor）。
stage 集成在后续 phase 完成。
"""

from haa.config import SSHConfig
from haa.remote.executor import ExecutionResult, RemoteExecutor
from haa.remote.transport import SSHError, SSHTransport

__all__ = [
    "SSHConfig",
    "SSHError",
    "SSHTransport",
    "ExecutionResult",
    "RemoteExecutor",
]
