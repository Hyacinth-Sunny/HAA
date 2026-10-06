"""视觉理解模块——调用 GLM-4.6V 描述论文插图。

DeepSeek v4 纯文本无法理解架构图/实验图表。此模块通过 GLM-4.6V（智谱 AI）
的 OpenAI 兼容接口获取图像描述，作为主模型的"眼睛"。
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("haa.vision")


class VisionError(RuntimeError):
    """图像理解调用失败。"""


def describe_image(
    image_path: str | None = None,
    image_url: str | None = None,
    prompt: str = "详细描述这张图片的内容，包括图表数据、架构、流程等所有可见信息。",
    config: Any = None,
) -> str:
    """调用 GLM-4.6V 描述图像（image_path 或 image_url 二选一）。"""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise VisionError("openai package not installed") from exc

    # Validate source BEFORE checking API key — parameter validation should
    # fail fast regardless of whether credentials are configured.
    if not image_path and not image_url:
        raise VisionError("describe_image: image_path or image_url required")

    model = getattr(config, "model", "glm-4.6v") if config else "glm-4.6v"
    api_base = (
        getattr(config, "api_base", "https://open.bigmodel.cn/api/paas/v4")
        if config
        else "https://open.bigmodel.cn/api/paas/v4"
    )
    api_key_env = getattr(config, "api_key_env", "ZHIPU_API_KEY") if config else "ZHIPU_API_KEY"
    max_tokens = getattr(config, "max_tokens", 2000) if config else 2000

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise VisionError(f"env var {api_key_env} not set")

    client = OpenAI(api_key=api_key, base_url=api_base)

    if image_url:
        image_content = {"type": "image_url", "image_url": {"url": image_url}}
    elif image_path:
        p = Path(image_path)
        if not p.exists():
            raise VisionError(f"image not found: {image_path}")
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        ext = p.suffix.lstrip(".").lower()
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/png")
        image_content = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    else:
        raise VisionError("describe_image: image_path or image_url required")

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [image_content, {"type": "text", "text": prompt}],
            }],
            max_tokens=max_tokens,
            timeout=60,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        raise VisionError(f"vision model {model} call failed: {exc}") from exc
