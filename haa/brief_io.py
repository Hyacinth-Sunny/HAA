"""Brief 加载统一入口（v1.0.2）。

此前 `haa/cli/main.py` 与 `haa/cli/project.py` 各有一份逐行相同的
`_load_brief`；且 `.md` 会落进 `yaml.safe_load` 直接报错——原版格式简报
不能直接投喂。本模块统一两处并增加 `.md` 编译路径（简报编译器）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from haa.models import Brief

logger = logging.getLogger("haa.brief_io")

_MARKDOWN_SUFFIXES = {".md", ".markdown"}


def load_brief(path: Path, *, llm=None) -> Brief:
    """Load a Brief from YAML/JSON, a dead-format Markdown, or compile free-form.

    ``.md`` 双轨（v1.0.4）：先试死格式确定性解析（零 LLM、零重述损失）；
    结构不符再走 :func:`haa.brief_compiler.compile_brief`（LLM + 审阅）。
    其余后缀沿用 YAML→JSON 回退。
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        from haa.brief_schema import try_parse_dead_format

        dead = try_parse_dead_format(text)
        if dead is not None:
            logger.info("loaded dead-format brief %s (deterministic, no LLM)", path.name)
            return dead
        from haa.brief_compiler import build_compiler_client, compile_brief

        client = llm or build_compiler_client()
        logger.info("compiling free-form brief %s via LLM…", path.name)
        return compile_brief(text, client)
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif path.suffix == ".json":
        data = json.loads(text)
    else:
        try:
            data = yaml.safe_load(text)
        except Exception:
            data = json.loads(text)
    return Brief.model_validate(data)
