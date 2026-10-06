"""实验结果可视化——matplotlib 绘图 + GLM-4.6V 交叉验证。

LLM 生成绘图脚本 → 本地执行 → 调 GLM-4.6V 识别生成的图 → 返回路径+验证。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from haa.llm.vision import VisionError, describe_image

logger = logging.getLogger("haa.visualization")


def generate_chart(code: str, output_path: str, cwd: str | None = None) -> dict[str, Any]:
    """执行 matplotlib/seaborn 脚本生成图表。"""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if "savefig" not in code:
        code = code + (
            f"\nimport matplotlib.pyplot as plt\n"
            f"plt.savefig('{output}', dpi=150, bbox_inches='tight')\nplt.close()"
        )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        script_path = f.name
    try:
        proc = subprocess.run(
            ["python", script_path],
            capture_output=True, text=True, timeout=120,
            cwd=cwd or str(output.parent),
            env={**os.environ, "MPLBACKEND": "Agg"},
        )
        if proc.returncode != 0:
            return {"success": False, "image_path": "", "error": proc.stderr[:1000]}
        if not output.exists():
            return {"success": False, "image_path": "", "error": "savefig 未生成图片"}
        return {"success": True, "image_path": str(output), "error": ""}
    except subprocess.TimeoutExpired:
        return {"success": False, "image_path": "", "error": "绘图脚本超时（120s）"}
    finally:
        Path(script_path).unlink(missing_ok=True)


def verify_chart(image_path: str, expected_description: str, config: Any = None) -> dict[str, Any]:
    """调 GLM-4.6V 验证图表与预期是否一致。"""
    verify_prompt = (
        f"这是一张数据可视化图表。预期内容：{expected_description}\n\n"
        "请检查：1)图表是否正确反映了预期数据趋势 2)坐标轴/图例/标题是否清晰 "
        "3)是否有数据错误或误导。"
        '返回 JSON: {"verified": true/false, "description": "实际内容", "issues": "问题（如有）"}'
    )
    try:
        result = describe_image(image_path=image_path, prompt=verify_prompt, config=config)
        match = re.search(r"\{[^{}]+\}", result, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "verified": bool(data.get("verified", False)),
                "description": str(data.get("description", "")),
                "issues": str(data.get("issues", "")),
            }
        return {"verified": False, "description": result, "issues": "无法解析验证结果"}
    except VisionError as exc:
        return {"verified": False, "description": "", "issues": f"视觉验证失败: {exc}"}
    except Exception as exc:
        return {"verified": False, "description": "", "issues": f"解析失败: {exc}"}
