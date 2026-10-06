"""简报编译器 —— 原版格式研究简报（自由 Markdown）直接投喂 HAA。

背景（v1.0.2）：Brief 模型只吃结构化 YAML，用户原版简报是自由格式中文
Markdown，此前靠 Claude Code 人工二次转写——转写质量成为管线天花板且
不可复现。本模块把转写变成管线的一等公民：

- LLM 单次调用抽取五字段 + knowledge_files（prompt 铁律：约束逐字保留）
- 确定性后处理：regex 扫描文档中的文件路径，与 LLM 结果取并集（LLM 漏了
  就补上）——知识文件是全链路整合的原料，一条都不能丢
- 编译产物经用户审阅（GUI 预填表单 / CLI 打印）后才生效
"""

from __future__ import annotations

import logging
import re
from typing import Any

from haa.models import Brief
from haa.prompts import render_prompt

logger = logging.getLogger("haa.brief_compiler")

# 文件路径：绝对路径或 ./ 相对路径，带文档后缀（含中文文件名）。裸文件名
# （无 / 或 ./ 前缀）不收——与行文里的普通词无法区分。
_PATH_RE = re.compile(
    r"(?<![\w/])(?:/|\./)[\w\-./一-鿿]*?\.(?:md|pdf|txt|yaml|yml|json)\b"
)


def scan_knowledge_paths(markdown_text: str) -> list[str]:
    """Deterministic sweep for file paths mentioned in the document."""
    found = []
    for m in _PATH_RE.finditer(markdown_text or ""):
        p = m.group(0)
        if p not in found:
            found.append(p)
    return found


def compile_brief(markdown_text: str, llm_client: Any) -> Brief:
    """Compile a free-form research brief into a :class:`Brief`.

    ``llm_client`` is an :class:`~haa.llm.client.LLMClient` (or test double
    exposing ``call_json``). One JSON-mode call; deterministic path-scan union
    applied afterwards.
    """
    prompt = render_prompt("brief_compile")
    data, _usage = llm_client.call_json(
        [{"role": "user", "content": prompt + "\n\n---\n\n# 原版研究简报\n\n" + markdown_text}],
        stage="PR",
    )

    # 确定性并集：LLM 漏掉的知识路径由 regex 兜底（原料完整性优先）。
    scanned = scan_knowledge_paths(markdown_text)
    declared = [str(p) for p in (data.get("knowledge_files") or [])]
    knowledge = list(dict.fromkeys(declared + [p for p in scanned if p not in declared]))

    brief = Brief(
        title=str(data.get("title") or "").strip() or "Untitled compiled brief",
        problem_area=str(data.get("problem_area") or "").strip(),
        constraints=[str(c) for c in (data.get("constraints") or [])],
        exclusions=[str(e) for e in (data.get("exclusions") or [])],
        track=str(data.get("track") or "theory").strip().lower(),
        knowledge_files=knowledge,
    )
    if not brief.problem_area:
        raise ValueError("brief compiler: LLM returned an empty problem_area")
    n_extra = len(knowledge) - len(declared)
    if n_extra > 0:
        logger.info("brief compiler: regex sweep added %d path(s) the LLM missed", n_extra)
    return brief


def build_compiler_client() -> Any:
    """Build an LLMClient from the loaded config (CLI/Web compile entry)."""
    import os

    from haa.config import load_config
    from haa.llm.client import LLMClient

    cfg = load_config()
    c = cfg.llm
    api_key = os.environ.get(c.api_key_env) or None
    return LLMClient(
        model=c.model,
        timeout=cfg.timeouts.llm,
        wall_timeout=cfg.timeouts.llm_wall,
        max_retries=c.max_retries,
        api_base=c.api_base or None,
        api_key=api_key,
    )
