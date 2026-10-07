"""M1 工具①：exec_bash / job_output / job_kill（翻译清单 §1，26 例）。

公共构造：原生接管注册表（路径白名单=临时目录、写闸门开、命令黑名单挂链）。
"""
from __future__ import annotations

import time

import pytest

from haa.harness.checklist import Checklist, PathWhitelist, ReadBeforeWriteGate
from haa.harness.registry import ToolError, ToolRegistry, ToolMenu
from haa.harness.session_log import SessionEventLog, _RecordingSink
from haa.harness.tools import exec_bash as B
from haa.harness.tools.exec_bash import ForegroundRunner
from haa.harness.tools.native import apply_native_tools, native_prompt_sections
from haa.harness.prompt_sections import FLOOR_TOOL_RULES


def native_reg(tmp_path, *, gate=True):
    reg = ToolRegistry(checklist=Checklist(
        session_log=SessionEventLog(_RecordingSink()),
        prechecks=[PathWhitelist(allowed_roots=(tmp_path,))],
        write_gate=ReadBeforeWriteGate(enabled=gate),
    ), menu=ToolMenu(None))
    apply_native_tools(reg, allowed_roots=(tmp_path,))
    return reg


# ---------------- 1a 前台执行 ----------------

def test_01_success_returns_stdout_with_exit_marker(tmp_path):
    r = native_reg(tmp_path).invoke("exec_bash", {"command": "echo hello"})
    assert "hello" in r.content and "[exit code: 0]" in r.content


def test_02_silent_command_renders_no_output(tmp_path):
    r = native_reg(tmp_path).invoke("exec_bash", {"command": "true"})
    assert "(no output)" in r.content and "[exit code: 0]" in r.content


def test_03_stderr_sections_marked(tmp_path):
    r = native_reg(tmp_path).invoke("exec_bash", {"command": "echo err 1>&2"})
    assert "[stderr]" in r.content and "err" in r.content


def test_04_nonzero_exit_marker_not_tool_error(tmp_path):
    r = native_reg(tmp_path).invoke("exec_bash", {"command": "exit 3"})
    assert "[exit code: 3]" in r.content


def test_05_timeout_kills_process_group_both_markers_ordered(tmp_path):
    r = native_reg(tmp_path).invoke(
        "exec_bash", {"command": "sleep 30 & sleep 30", "timeout": 1})
    assert "[timed out after" in r.content
    assert "[killed by signal" in r.content
    assert r.content.index("[timed out after") < r.content.index("[killed by signal")


def test_06_timeout_reported_even_when_signal_trapped_exit0(tmp_path):
    r = native_reg(tmp_path).invoke(
        "exec_bash", {"command": "trap 'exit 0' TERM; sleep 30", "timeout": 1})
    assert "[timed out after" in r.content
    assert "[exit code" not in r.content


def test_07_truncation_head25_tail75_with_marker(tmp_path):
    r = native_reg(tmp_path).invoke(
        "exec_bash",
        {"command": "printf 'HEAD'; head -c 40000 /dev/zero | tr '\\0' 'x'; printf 'TAIL'"},
    )
    assert "省略" in r.content and "HEAD" in r.content and "TAIL" in r.content
    assert len(r.content.encode()) < 36_000  # 32KB 帽 + 标记余量


def test_08_workdir_must_be_within_whitelist(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="outside sandbox roots"):
        reg.invoke("exec_bash", {"command": "pwd", "cwd": "/etc"})
    r = reg.invoke("exec_bash", {"command": "pwd", "cwd": str(tmp_path)})
    assert str(tmp_path) in r.content


def test_09_spawn_failure_is_loud(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="No such file"):
        reg.invoke("exec_bash", {"command": "pwd", "cwd": str(tmp_path / "nope")})
    # 启动失败=响亮异常（DSH ENOENT 语义），绝不静默


def test_10_schema_rejects_bad_args(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="non-empty"):
        reg.invoke("exec_bash", {"command": "   "})
    with pytest.raises(ToolError, match="non-empty"):
        reg.invoke("exec_bash", {})


def test_11_env_whitelist_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("HAA_TEST_SECRET", "leak-me")
    reg = native_reg(tmp_path)
    r = reg.invoke("exec_bash", {"command": "printenv HAA_TEST_SECRET; true"})
    assert "leak-me" not in r.content  # 白名单外不透传
    r2 = reg.invoke("exec_bash", {"command": "test -n \"$PATH\" && echo path-ok"})
    assert "path-ok" in r2.content    # 白名单内保留


def test_12_per_call_timeout_capped_at_max():
    fr = ForegroundRunner(max_timeout_ms=5000)
    assert fr.clamp_timeout(10 ** 9) == 5000
    assert fr.clamp_timeout(100) == 100
    assert fr.clamp_timeout("bad") == 5000  # 非法值落默认后被上限钳制


# ---------------- 1b 后台任务 ----------------

def _bg(reg, tmp_path, cmd):
    return reg.invoke(
        "exec_bash", {"command": cmd, "run_in_background": True, "cwd": str(tmp_path)},
    ).meta["job_id"]


def test_13_run_in_background_returns_job_id(tmp_path):
    reg = native_reg(tmp_path)
    r = reg.invoke("exec_bash", {"command": "echo bg-ok", "run_in_background": True,
                                 "cwd": str(tmp_path)})
    assert "started background job bash-1" in r.content
    assert r.meta["job_id"] == "bash-1"


def test_14_15_job_output_incremental_consuming_then_empty(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "echo hello-bg")
    snap = reg.native.jobs.get(jid).wait(timeout_s=10)
    assert snap.state == "completed"
    r1 = reg.invoke("job_output", {"job_id": jid})
    assert "hello-bg" in r1.content and "[status: completed" in r1.content
    r2 = reg.invoke("job_output", {"job_id": jid})
    assert "hello-bg" not in r2.content  # 增量不重复投递


def test_16_job_output_stderr_marked(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "echo bgerr 1>&2")
    reg.native.jobs.get(jid).wait(timeout_s=10)
    r = reg.invoke("job_output", {"job_id": jid})
    assert "[stderr]" in r.content


def test_17_job_kill_settles_then_already_finished(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "sleep 60")
    r = reg.invoke("job_kill", {"job_id": jid})
    assert "cancellation requested" in r.content
    reg.native.jobs.get(jid).wait(timeout_s=10)
    r2 = reg.invoke("job_kill", {"job_id": jid})
    assert "already-finished" in r2.content


def test_18_job_kill_grace_escalation_sigkill(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "trap '' TERM; sleep 30")
    time.sleep(0.5)
    job = reg.native.jobs.get(jid)
    assert job.kill(grace_s=0.5) is True
    job.wait(timeout_s=10)
    assert job.state == "killed"


def test_19_self_kill_settles_as_killed(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "kill -TERM $$")
    snap = reg.native.jobs.get(jid).wait(timeout_s=10)
    assert snap.state == "killed"


def test_20_dead_process_reported_not_hung(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "echo done-quick")
    reg.native.jobs.get(jid).wait(timeout_s=10)
    r = reg.invoke("job_output", {"job_id": jid})
    assert "[status: completed" in r.content  # 死亡→状态行，不挂起


def test_21_unknown_or_empty_job_id_errors(tmp_path):
    reg = native_reg(tmp_path)
    with pytest.raises(ToolError, match="unknown job"):
        reg.invoke("job_output", {"job_id": "bash-99"})
    with pytest.raises(ToolError, match="non-empty"):
        reg.invoke("job_output", {"job_id": " "})


def test_22_wait_blocks_with_cap_not_killing(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "sleep 5")
    reg.native.max_wait_s = 0.3
    t0 = time.monotonic()
    r = reg.invoke("job_output", {"job_id": jid, "wait": True, "wait_timeout_s": 999})
    assert time.monotonic() - t0 < 3  # 用户给的 999s 被帽钳制
    assert "[status: running" in r.content  # 任务仍活，未被 wait 杀掉
    reg.invoke("job_kill", {"job_id": jid})


def test_23_output_limit_bounds_body_and_status(tmp_path):
    reg = native_reg(tmp_path)
    jid = _bg(reg, tmp_path, "head -c 70000 /dev/zero | tr '\\0' 'y'")
    reg.native.jobs.get(jid).wait(timeout_s=20)
    r = reg.invoke("job_output", {"job_id": jid})
    assert len(r.content.encode()) < 36_000  # 正文+状态行同受 32KB 纪律


def test_24_25_concurrent_ten_jobs_and_cap(tmp_path):
    reg = native_reg(tmp_path)
    ids = [_bg(reg, tmp_path, f"sleep 5") for _ in range(10)]
    assert len(set(ids)) == 10
    with pytest.raises(ToolError, match="limit reached"):
        _bg(reg, tmp_path, "sleep 5")
    for jid in ids:
        reg.invoke("job_kill", {"job_id": jid})


def test_26_exit_code_habit_prompt_section():
    assert "[exit code" in B.EXIT_CODE_HABIT
    secs = native_prompt_sections()
    tool_secs = [s for s in secs if FLOOR_TOOL_RULES[0] <= s.floor <= FLOOR_TOOL_RULES[1]]
    assert any("exit code" in s.text for s in tool_secs)
