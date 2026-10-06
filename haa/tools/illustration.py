"""论文插图生成——调用 nano-banana-2 API 生成精美插图（架构图/概念示意图）。

通过 OpenAI 兼容接口调用 nano-banana-2。
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("haa.illustration")


class IllustrationError(RuntimeError):
    pass


def _aspect_to_size(aspect: str) -> str:
    return {
        "16:9": "1536x864", "9:16": "864x1536",
        "1:1": "1024x1024", "4:3": "1024x768", "3:4": "768x1024",
    }.get(aspect, "1024x1024")


def generate_illustration(
    prompt: str,
    output_path: str,
    aspect_ratio: str = "16:9",
    config: Any = None,
) -> str:
    """调 nano-banana-2 生成论文插图，返回保存路径。"""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise IllustrationError("openai package not installed") from exc

    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("ILLUSTRATION_API_BASE", "https://api.gpt.ge/v1")
    model = os.environ.get("ILLUSTRATION_MODEL", "nano-banana-2")
    if not api_key:
        raise IllustrationError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key, base_url=api_base)
    enhanced = (
        f"{prompt}\n\n"
        "Style: clean, professional academic paper illustration. "
        "High quality, minimal background, suitable for publication."
    )
    try:
        resp = client.images.generate(
            model=model, prompt=enhanced, n=1,
            size=_aspect_to_size(aspect_ratio), response_format="b64_json",
        )
        image_data = base64.b64decode(resp.data[0].b64_json)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image_data)
        logger.info("Illustration saved to %s", out)
        return str(out)
    except Exception as exc:
        raise IllustrationError(f"nano-banana-2 call failed: {exc}") from exc
