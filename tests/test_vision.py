"""视觉理解工具测试（mock OpenAI client）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from haa.config import VisionConfig
from haa.llm.vision import VisionError, describe_image


def test_describe_image_no_api_key(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    with pytest.raises(VisionError, match="not set"):
        describe_image(image_url="http://example.com/img.png", config=VisionConfig())


def test_describe_image_url_calls_openai(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "fake-key")
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="A bar chart showing trends."))]

    with patch("openai.OpenAI") as mock_openai:
        client = mock_openai.return_value
        client.chat.completions.create.return_value = mock_resp

        result = describe_image(
            image_url="http://x/img.png", config=VisionConfig()
        )
        assert "bar chart" in result
        # verify call used the configured model
        call_kwargs = client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "glm-4.6v"


def test_describe_image_path_not_found(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "fake-key")
    with pytest.raises(VisionError, match="not found"):
        describe_image(image_path="/nonexistent/img.png", config=VisionConfig())


def test_describe_image_requires_source():
    with pytest.raises(VisionError, match="image_path or image_url"):
        describe_image(config=VisionConfig())


def test_describe_image_via_registry_permission():
    """describe_image 不在 VERIFY/GRADE（纯逻辑判断阶段不看图）。"""
    from haa.config import ToolsConfig
    from haa.llm.tools import ToolPermissionError, ToolRegistry

    reg = ToolRegistry(ToolsConfig(), campaigns_dir="/tmp")
    with pytest.raises(ToolPermissionError):
        reg.execute("describe_image", {"image_url": "http://x"}, stage_name="VERIFY")
    with pytest.raises(ToolPermissionError):
        reg.execute("describe_image", {"image_url": "http://x"}, stage_name="GRADE")
