"""插图生成工具测试（mock OpenAI images.generate）。"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from haa.tools.illustration import IllustrationError, generate_illustration


def test_generate_illustration_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("ILLUSTRATION_API_BASE", "https://fake.api/v1")
    fake_b64 = base64.b64encode(b"fake png data").decode()
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(b64_json=fake_b64)]
    with patch("openai.OpenAI") as mock_openai:
        client = mock_openai.return_value
        client.images.generate.return_value = mock_resp
        out_path = str(tmp_path / "fig.png")
        result = generate_illustration("architecture diagram", out_path)
    assert result == out_path
    assert (tmp_path / "fig.png").read_bytes() == b"fake png data"


def test_generate_illustration_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(IllustrationError, match="not set"):
        generate_illustration("test", "/tmp/x.png")


def test_illustration_permission():
    """illustration_drawing 只在 WRITE/REFINE 可用。"""
    from haa.config import ToolsConfig
    from haa.llm.tools import ToolPermissionError, ToolRegistry

    reg = ToolRegistry(ToolsConfig(), campaigns_dir="/tmp")
    with pytest.raises(ToolPermissionError):
        reg.execute("illustration_drawing", {"prompt": "x"}, stage_name="DESIGN")
