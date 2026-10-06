"""Tests for ClaudeCodeACP — the HAA↔Claude Code coding channel (v0.6).

All tests mock subprocess.run so no real acpx / Claude Code calls are made.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haa.coding_agent import (
    ACPResult,
    ClaudeCodeACP,
    detect_acpx_path,
    _format_codegen_prompt,
)
from haa.config import ACPConfig


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _completed(stdout="", stderr="", returncode=0):
    """Build a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _make_acp(tmp_path, **config_overrides) -> ClaudeCodeACP:
    cfg = ACPConfig(**config_overrides) if config_overrides else ACPConfig()
    return ClaudeCodeACP(
        campaign_id="test-campaign",
        work_dir=tmp_path / "work",
        config=cfg,
    )


# --------------------------------------------------------------------------- #
#  detect_acpx_path
# --------------------------------------------------------------------------- #

class TestDetectAcpxPath:

    def test_explicit_configured_path_used(self, tmp_path):
        fake_bin = tmp_path / "myacpx"
        fake_bin.write_text("#!/bin/bash\necho fake")
        result = detect_acpx_path(str(fake_bin))
        assert result == str(fake_bin)

    def test_nonexistent_configured_falls_through(self):
        # Should not use a nonexistent path; falls through to detection.
        result = detect_acpx_path("/nonexistent/acpx")
        # Result is some detected path or "npx acpx" — just verify it's not the bad path.
        assert result != "/nonexistent/acpx"

    def test_returns_string(self):
        result = detect_acpx_path()
        assert isinstance(result, str)
        assert len(result) > 0


# --------------------------------------------------------------------------- #
#  Session management
# --------------------------------------------------------------------------- #

class TestSessionManagement:

    def test_ensure_session_creates_when_missing(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            # sessions show → rc=1 (not found); sessions new → rc=0 (created).
            mock_run.side_effect = [_completed(returncode=1), _completed(returncode=0)]
            acp._ensure_session()
        # Two calls: show (fail) + new (create).
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0] == ["claude", "sessions", "show", acp.session_name]
        assert mock_run.call_args_list[1].args[0] == ["claude", "sessions", "new", "--name", acp.session_name]

    def test_ensure_session_reuses_when_exists(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            # sessions show → rc=0 (exists).
            mock_run.return_value = _completed(returncode=0)
            acp._ensure_session()
        # Only one call: show (found). No new.
        assert mock_run.call_count == 1

    def test_ensure_session_idempotent(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(returncode=0)
            acp._ensure_session()
            acp._ensure_session()  # second call should be a no-op
        assert mock_run.call_count == 1

    def test_session_name_includes_campaign_id(self, tmp_path):
        acp = _make_acp(tmp_path)
        assert "test-campaign" in acp.session_name

    def test_close_calls_acpx(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(returncode=0)
            acp._ensure_session()
            acp.close()
        # show + new (or show only) + close.
        close_call = mock_run.call_args_list[-1]
        assert "close" in close_call.args[0]


# --------------------------------------------------------------------------- #
#  Prompt sending
# --------------------------------------------------------------------------- #

class TestSendPrompt:

    def test_short_prompt_sent_inline(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(stdout="done", returncode=0)
            result = acp.send_prompt("fix the bug")
        assert result.success
        assert "fix the bug" in mock_run.call_args.args[0]

    def test_long_prompt_uses_file(self, tmp_path):
        acp = _make_acp(tmp_path)
        long_prompt = "x" * 600  # > 500 chars → file mode
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(stdout="done", returncode=0)
            result = acp.send_prompt(long_prompt)
        assert result.success
        # The -f flag should be present.
        cmd = mock_run.call_args.args[0]
        assert "-f" in cmd

    def test_timeout_returns_failure(self, tmp_path):
        acp = _make_acp(tmp_path)
        acp._session_ensured = True  # skip session check; test the send path only
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="acpx", timeout=1)
            result = acp.send_prompt("test", timeout=1)
        assert not result.success
        assert result.timed_out

    def test_nonzero_returncode_is_failure(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(returncode=1, stderr="error")
            result = acp.send_prompt("test")
        assert not result.success
        assert result.returncode == 1


# --------------------------------------------------------------------------- #
#  High-level wrappers
# --------------------------------------------------------------------------- #

class TestHighLevelWrappers:

    def test_generate_code_sends_prompt_and_sets_work_dir(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "send_prompt") as mock_send:
            mock_send.return_value = ACPResult(success=True, output="code written")
            result = acp.generate_code(
                paper_precursor={"candidate_title": "Test Paper", "paper": {"abstract": "We show..."}},
                exp_spec={"datasets": ["MNIST"]},
            )
        assert result.success
        assert result.work_dir == acp.work_dir
        mock_send.assert_called_once()
        # The prompt should contain key info.
        prompt_arg = mock_send.call_args.args[0]
        assert "Test Paper" in prompt_arg
        assert "MNIST" in prompt_arg

    def test_fix_traceback_sends_traceback(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "send_prompt") as mock_send:
            mock_send.return_value = ACPResult(success=True)
            acp.fix_traceback("Traceback (most recent call last):\n  File ...")
        prompt = mock_send.call_args.args[0]
        assert "Traceback" in prompt
        assert "Fix" in prompt

    def test_diagnose_metrics_sends_metrics(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "send_prompt") as mock_send:
            mock_send.return_value = ACPResult(success=True)
            acp.diagnose_metrics(
                metrics={"final_loss": 999.0, "accuracy": 0.01},
                log_tail="epoch 10: loss=999.0",
            )
        prompt = mock_send.call_args.args[0]
        assert "999" in prompt
        assert "anomalous" in prompt.lower() or "diagnose" in prompt.lower()


# --------------------------------------------------------------------------- #
#  Context manager
# --------------------------------------------------------------------------- #

class TestContextManager:

    def test_context_manager_creates_and_closes(self, tmp_path):
        acp = _make_acp(tmp_path)
        with patch.object(acp, "_run_acpx") as mock_run:
            mock_run.return_value = _completed(returncode=0)
            with acp:
                pass  # session ensured on enter, closed on exit
        # At least: show (session check) + close.
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any("show" in c for c in calls)
        assert any("close" in c for c in calls)


# --------------------------------------------------------------------------- #
#  Prompt formatting
# --------------------------------------------------------------------------- #

class TestPromptFormatting:

    def test_codegen_prompt_contains_all_required_files(self, tmp_path):
        prompt = _format_codegen_prompt(
            paper_precursor={"candidate_title": "X", "paper": {"abstract": "A", "method": "M"}},
            exp_spec={"datasets": ["D"]},
            work_dir=tmp_path,
        )
        for required in ["data.py", "model.py", "train.py", "eval.py", "requirements.txt", "run.sh"]:
            assert required in prompt
        assert "results/" in prompt

    def test_codegen_prompt_handles_missing_sections(self, tmp_path):
        prompt = _format_codegen_prompt(
            paper_precursor={},
            exp_spec={},
            work_dir=tmp_path,
        )
        assert "data.py" in prompt  # requirements still listed
