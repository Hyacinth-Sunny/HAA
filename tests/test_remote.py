"""Phase B SSH 远程执行测试（mock subprocess / transport 方法，不发真实 SSH）。"""

from __future__ import annotations

import subprocess
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from haa.config import SSHConfig
from haa.remote.executor import RemoteExecutor
from haa.remote.transport import SSHError, SSHTransport


def _cfg():
    return SSHConfig(host="testhost", user="testuser", key_path="/tmp/fake_key")


def _ok_proc(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


# --- 1. SSHTransport.run 正常 -------------------------------------------------

def test_ssh_run_ok():
    t = SSHTransport(_cfg())
    with patch("haa.remote.transport.subprocess.run", return_value=_ok_proc("result\n")):
        assert t.run("ls") == "result\n"


# --- 2. SSHTransport.run 远程命令失败 → SSHError ------------------------------

def test_ssh_run_fail():
    t = SSHTransport(_cfg())
    with patch("haa.remote.transport.subprocess.run", return_value=_ok_proc("", returncode=1)):
        with pytest.raises(SSHError):
            t.run("badcmd")


# --- 3. SSHTransport.run 命令超时 → SSHError ---------------------------------

def test_ssh_run_timeout():
    t = SSHTransport(_cfg())
    with patch("haa.remote.transport.subprocess.run",
               side_effect=subprocess.TimeoutExpired("cmd", 60)):
        with pytest.raises(SSHError):
            t.run("slow")


# --- 4. SSHTransport.upload 正常 ---------------------------------------------

def test_ssh_upload_ok():
    t = SSHTransport(_cfg())
    with patch("haa.remote.transport.subprocess.run", return_value=_ok_proc()):
        t.upload("/local/file", "/remote/file")  # 不 raise 即 OK


# --- 5. RemoteExecutor.deploy_and_run 完整流程（mock transport 方法）---------

def test_deploy_and_run(tmp_path):
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    (code_dir / "main.py").write_text("print('hi')")
    (code_dir / "requirements.txt").write_text("")

    exe = RemoteExecutor(_cfg())
    with patch.object(exe.transport, "run", return_value=""), \
         patch.object(exe.transport, "upload"), \
         patch.object(exe.transport, "run_background", return_value="12345"), \
         patch.object(exe.transport, "poll_process",
                      return_value={"log_tail": "done", "completed": True}), \
         patch.object(exe.transport, "download"):
        result = exe.deploy_and_run("camp1", str(code_dir))

    assert result.success is True
    assert result.pid == "12345"


# --- 6. _pack_code 排除 __pycache__ 等 ---------------------------------------

def test_pack_code_excludes(tmp_path):
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    (code_dir / "main.py").write_text("x")
    pycache = code_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "junk.pyc").write_text("y")

    pkg = RemoteExecutor._pack_code(str(code_dir), "test")
    with tarfile.open(pkg, "r:gz") as tar:
        names = tar.getnames()
    assert "main.py" in names
    assert "__pycache__" not in names
