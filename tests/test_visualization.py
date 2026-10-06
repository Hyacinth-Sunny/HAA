"""可视化工具测试（mock subprocess + vision describe_image）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from haa.tools.visualization import generate_chart, verify_chart


def test_generate_chart_ok(tmp_path):
    out = tmp_path / "chart.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake png")  # 模拟脚本生成了图片
    with patch("haa.tools.visualization.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = generate_chart("import matplotlib.pyplot as plt", str(out))
    assert result["success"] is True
    assert result["image_path"] == str(out)


def test_generate_chart_fail(tmp_path):
    out = tmp_path / "bad.png"
    with patch("haa.tools.visualization.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="SyntaxError")
        result = generate_chart("bad code", str(out))
    assert result["success"] is False
    assert "SyntaxError" in result["error"]


def test_verify_chart_parses_json():
    with patch(
        "haa.tools.visualization.describe_image",
        return_value='{"verified": true, "description": "correct trend", "issues": ""}',
    ):
        result = verify_chart("/fake/path.png", "expected upward trend")
    assert result["verified"] is True
    assert "correct trend" in result["description"]


def test_verify_chart_vision_error():
    from haa.llm.vision import VisionError

    with patch(
        "haa.tools.visualization.describe_image",
        side_effect=VisionError("connection failed"),
    ):
        result = verify_chart("/fake.png", "expected")
    assert result["verified"] is False
    assert "connection failed" in result["issues"]


def test_result_visualization_permission():
    """result_visualization 只在 WRITE/REFINE 可用。"""
    from haa.config import ToolsConfig
    from haa.llm.tools import ToolPermissionError, ToolRegistry

    reg = ToolRegistry(ToolsConfig(), campaigns_dir="/tmp")
    with pytest.raises(ToolPermissionError):
        reg.execute("result_visualization", {"code": "x"}, stage_name="SEEK")
    # WRITE 允许（会尝试执行 code，但权限检查通过）
    # 不实际执行，只验证权限不拦
