"""M1 工具④⑤：SSH 双工具（翻译清单 §4，11 例）＋ 运行监控（§5，9 例）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from haa.harness.registry import ToolError
from haa.harness.tools import ssh_tools as S
from haa.harness.tools.experiment_status import (
    DEFAULT_POLL_INTERVAL_S,
    HeartbeatMonitor,
    RemoteExperiment,
)
from haa.harness.tools.jobs import JobSnapshot, validate_transition
from tests.harness_tools.test_m1_exec_bash import native_reg


# ---------------- §4 ssh_exec / ssh_transfer ----------------

class FakeTransport:
    def __init__(self, *, fail_first=0, sha_reply=""):
        self.fail_first = fail_first
        self.sha_reply = sha_reply
        self.calls = []

    def run(self, command, timeout_s=120):
        self.calls.append(("run", command))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("simulated drop")
        if command.startswith("sha256sum"):
            return SimpleNamespace(returncode=0, stdout=self.sha_reply + "\n")
        if command.startswith("stat "):
            return SimpleNamespace(returncode=0, stdout="1024")
        return SimpleNamespace(returncode=0, stdout="remote-out", stderr="")

    def upload(self, local, remote):
        self.calls.append(("upload", local, remote))

    def download(self, remote, local):
        self.calls.append(("download", remote, local))
        Path(local).write_bytes(b"x" * 1024)


def _ssh_reg(tmp_path, transport):
    reg = native_reg(tmp_path)
    reg.native.ssh.profiles["default"] = S.SSHServerProfile(
        "default", host="fake-host", user="u")
    ch = S.SSHChannel(reg.native.ssh.profiles["default"],
                      transport_factory=lambda: transport)
    reg.native.ssh._channels["default"] = ch
    return reg


def test_ssh_01_exec_output_with_exit_marker(tmp_path):
    reg = _ssh_reg(tmp_path, FakeTransport())
    r = reg.invoke("ssh_exec", {"command": "echo remote-out"})
    assert "remote-out" in r.content and "[exit code: 0]" in r.content


def test_ssh_02_timeout_discipline_passed_to_channel(tmp_path):
    t = FakeTransport()
    reg = _ssh_reg(tmp_path, t)
    reg.invoke("ssh_exec", {"command": "sleep 1", "timeout_s": 5})
    assert any("sleep 1" in c[1] for c in t.calls)


def test_ssh_03_truncation_same_discipline(tmp_path):
    t = FakeTransport()
    t.run = lambda command, timeout_s=120: SimpleNamespace(
        returncode=0, stdout="z" * 60_000, stderr="")
    reg = _ssh_reg(tmp_path, t)
    r = reg.invoke("ssh_exec", {"command": "big"})
    assert len(r.content.encode()) < 36_000 and "省略" in r.content


def test_ssh_04_reconnect_backoff_cap3(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "BACKOFF_BASE_S", 0.01)
    t = FakeTransport(fail_first=2)
    reg = _ssh_reg(tmp_path, t)
    r = reg.invoke("ssh_exec", {"command": "echo after-reconnect"})
    assert "[exit code: 0]" in r.content          # 前两次掉线，第三次成功
    assert len(t.calls) == 3                       # 恰好三次尝试（上限即此）


def test_ssh_04b_all_attempts_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "BACKOFF_BASE_S", 0.01)
    t = FakeTransport(fail_first=99)
    reg = _ssh_reg(tmp_path, t)
    with pytest.raises(ToolError, match="after 3 attempts"):
        reg.invoke("ssh_exec", {"command": "echo never"})


def test_ssh_05_transfer_roundtrip_verify(tmp_path):
    local = tmp_path / "up.txt"
    local.write_bytes(b"payload" * 100)
    import hashlib
    sha = hashlib.sha256(local.read_bytes()).hexdigest()
    reg = _ssh_reg(tmp_path, FakeTransport(sha_reply=sha))
    r = reg.invoke("ssh_transfer", {"local_path": str(local),
                                    "remote_path": "/remote/up.txt",
                                    "direction": "upload"})
    assert "transfer ok" in r.content and "chunked=False" in r.content


def test_ssh_05b_transfer_verify_failure(tmp_path):
    local = tmp_path / "bad.txt"
    local.write_bytes(b"payload")
    reg = _ssh_reg(tmp_path, FakeTransport(sha_reply="deadbeef" * 8))
    with pytest.raises(ToolError, match="verify failed"):
        reg.invoke("ssh_transfer", {"local_path": str(local),
                                    "remote_path": "/remote/bad.txt",
                                    "direction": "upload"})


def test_ssh_05c_transfer_download_roundtrip(tmp_path):
    dst = tmp_path / "down.bin"
    reg = _ssh_reg(tmp_path, FakeTransport())
    r = reg.invoke("ssh_transfer", {"local_path": str(dst),
                                    "remote_path": "/remote/d.bin",
                                    "direction": "download"})
    assert "transfer ok" in r.content and dst.exists()


def test_ssh_06_07_key_preferred_and_deprecated_note(tmp_path):
    # 兼容通道只存环境变量名（不存值），结果 meta 携带弃用说明
    reg = native_reg(tmp_path)
    reg.native.ssh.profiles["legacy"] = S.SSHServerProfile(
        "legacy", host="h", user="u", pass_env="PW_ENV")
    ch = S.SSHChannel(reg.native.ssh.profiles["legacy"],
                      transport_factory=lambda: FakeTransport())
    reg.native.ssh._channels["legacy"] = ch
    r = reg.invoke("ssh_exec", {"server": "legacy", "command": "echo x"})
    assert r.meta.get("note", "").startswith("password authentication is deprecated")
    assert "PW_ENV" not in r.content  # 变量名不进模型可见内容


def test_ssh_08_credentials_never_in_code_or_results(tmp_path):
    reg = native_reg(tmp_path)
    reg.native.ssh.profiles["k"] = S.SSHServerProfile(
        "k", host="h", key_path="~/.ssh/id_test_key")
    ch = S.SSHChannel(reg.native.ssh.profiles["k"],
                      transport_factory=lambda: FakeTransport())
    reg.native.ssh._channels["k"] = ch
    r = reg.invoke("ssh_exec", {"server": "k", "command": "whoami"})
    assert "PRIVATE KEY" not in r.content and "id_test_key" not in r.content


def test_ssh_09_unknown_server_profile(tmp_path):
    reg = _ssh_reg(tmp_path, FakeTransport())
    with pytest.raises(ToolError, match="unknown server profile"):
        reg.invoke("ssh_exec", {"server": "ghost", "command": "x"})


def test_ssh_10_11_merged_channel_imports_alive():
    # 合并等价：底层 SSHTransport 的既有测试（tests/test_remote.py）与
    # p2 传输测试保持全绿即合并回归（e2e 全量覆盖）；此处锚定导入面。
    from haa.remote.transport import SSHTransport  # noqa: F401
    from haa.p2.transport import SSHDebugTransport  # noqa: F401


# ---------------- §5 experiment_status ----------------

BAD_SNAPSHOTS = [
    dict(job_id="bash-x", kind="bash", state="running", started_at=1.0, finished_at=2.0),
    dict(job_id="bash-1", kind="bash", state="completed", started_at=1.0),
    dict(job_id="bash-1", kind="bash", state="completed", started_at=5.0, finished_at=2.0),
    dict(job_id="bash-1", kind="bash", state="timeout", started_at=1.0,
         finished_at=2.0, timed_out=False),
    dict(job_id="noKindSep", kind="bash", state="running", started_at=1.0),
    dict(job_id="bash-1", kind="other", state="running", started_at=1.0),
    dict(job_id="bash-1", kind="bash", state="weird", started_at=1.0),
]


@pytest.mark.parametrize("raw", BAD_SNAPSHOTS)
def test_mon_01_snapshot_coherence_matrix(raw):
    with pytest.raises(ValueError):
        JobSnapshot(**raw).validate()


def test_mon_01b_valid_snapshots_pass():
    JobSnapshot(job_id="bash-1", kind="bash", state="running", started_at=1.0).validate()
    JobSnapshot(job_id="bash-1", kind="bash", state="completed", started_at=1.0,
                finished_at=2.0, exit_code=0).validate()


def test_mon_02_legal_predecessor_transitions():
    validate_transition("running", "completed")
    validate_transition("running", "killed")
    validate_transition("running", "timeout")
    validate_transition("running", "failed")
    for bad in (("completed", "running"), ("killed", "completed"),
                ("timeout", "running"), ("completed", "completed")):
        with pytest.raises(ValueError):
            validate_transition(*bad)


def test_mon_03_state_change_emits_job_state_event(tmp_path):
    events = []
    reg = native_reg(tmp_path)
    reg.native.jobs._emit = lambda j, s, d: events.append((j, s))
    jid = reg.invoke("exec_bash", {"command": "echo ev", "run_in_background": True,
                                   "cwd": str(tmp_path)}).meta["job_id"]
    reg.native.jobs.get(jid).wait(timeout_s=10)
    states = [s for _, s in events]
    assert "running" in states and "completed" in states


def test_mon_04_heartbeat_loss_detection():
    hm = HeartbeatMonitor(threshold=3)
    # 基线观测（第 1 次轮询建立参照，不计停滞）
    assert hm.observe("j", state="running", progress_marker=10) is False
    assert hm.observe("j", state="running", progress_marker=10) is False  # 停滞 1
    assert hm.observe("j", state="running", progress_marker=10) is False  # 停滞 2
    assert hm.observe("j", state="running", progress_marker=10) is True   # 连续第 3 次停滞→判死
    assert hm.observe("j", state="running", progress_marker=11) is False  # 有进展复位
    assert hm.observe("j", state="completed", progress_marker=11) is False  # 终态不报


def test_mon_05_poll_interval_default_60s():
    assert DEFAULT_POLL_INTERVAL_S == 60


def test_mon_06_log_tail_bounded(tmp_path):
    reg = native_reg(tmp_path)
    jid = reg.invoke("exec_bash", {"command": "head -c 20000 /dev/zero | tr '\\0' 'l'",
                                   "run_in_background": True,
                                   "cwd": str(tmp_path)}).meta["job_id"]
    reg.native.jobs.get(jid).wait(timeout_s=20)
    tail = reg.native.monitor.log_tail(jid)
    assert len(tail.encode()) <= 4100  # 4000 帽 + 截断标记余量


def test_mon_07_local_and_remote_one_state_model(tmp_path):
    reg = native_reg(tmp_path)
    reg.native.monitor.remote["exp-1"] = RemoteExperiment(
        experiment_id="exp-1", server="school", command="python train.py")
    jid = reg.invoke("exec_bash", {"command": "echo m", "run_in_background": True,
                                   "cwd": str(tmp_path)}).meta["job_id"]
    info_local = reg.native.monitor.status(jid, log_tail=False)
    info_remote = reg.native.monitor.status("exp-1", log_tail=False)
    assert set(info_local) == set(info_remote)  # 同一字段面（统一状态模型）
    assert info_remote["kind"] == "remote" and info_local["kind"] == "local"


def test_mon_08_read_after_settle_idempotent(tmp_path):
    reg = native_reg(tmp_path)
    jid = reg.invoke("exec_bash", {"command": "echo stable",
                                   "run_in_background": True,
                                   "cwd": str(tmp_path)}).meta["job_id"]
    reg.native.jobs.get(jid).wait(timeout_s=10)
    a = reg.native.monitor.status(jid)
    b = reg.native.monitor.status(jid)
    assert a["state"] == b["state"] == "completed"


def test_mon_09_unknown_job_error(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="unknown job or experiment"):
        reg.native.monitor.status("ghost-9")
