"""死格式研究简报解析器（v1.0.4）——固定结构的 Markdown 直接进管线，零 LLM。

设计动机（用户原始设想）：精心撰写的简报框定"死格式"（必有节+可选节），
HAA 确定性读取——省时、零重述损失、可 git diff 审阅。与编译器
（:mod:`haa.brief_compiler`，自由格式兜底）构成双轨：

    .md ──结构匹配死格式？──是──► parse_dead_format（零 LLM 快车道）
           └──否──────────────────► compile_brief（LLM + GUI 审阅）

死格式规范（见 data/研究简报模板.md）::

    # 研究简报：<标题>                    ← 必有（首个标题行）
    > 元信息：**赛道：** systems           ← 可选（缺省 theory）
    #### 一、问题阐述                      ← 必有 → problem_area §1
    #### 二、重难点分析                    ← 可选 → problem_area §2
    #### 三、已有研究进展                  ← 可选 → problem_area §3
                                           （文中文件路径 → knowledge_files）
    #### 四、产出要求                      ← 必有 → problem_area §4
    **硬约束**                             ← 必有：bullet 逐条 → constraints
    **明确不做**                           ← 可选：bullet → exclusions
    #### 五、思路拟定及可行性分析           ← 可选 → problem_area §5

节序号中文数字或阿拉伯数字均可；标题层级任意；"硬约束/明确不做"作为
加粗独立行（``**硬约束**``）或标题行（``### 硬约束``）均可。problem_area
= 各节正文**原文拼接**（硬约束/明确不做的 bullet 不重复计入——它们已是
结构化字段）。单遍行式状态机实现，无嵌套解析。
"""

from __future__ import annotations

import logging
import re

from haa.models import Brief

logger = logging.getLogger("haa.brief_schema")


class DeadFormatError(ValueError):
    """The markdown does not match the dead format（调用方应回退编译器）。"""


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SECTION_RE = re.compile(r"^([一二三四五六七八九十\d]+)\s*[、.．]\s*(.+?)\s*$")
_SUB_RE = re.compile(
    r"^(?:#{1,6}\s+)?\*{0,2}(硬约束|明确不做|排除方向)\*{0,2}\s*[:：]?\s*$"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、．])\s+(.+?)\s*$")
# 小节别名归一（"排除方向" 与 "明确不做" 同义）
_SUB_ALIAS = {"排除方向": "明确不做"}
_TRACK_RE = re.compile(
    r"(?:赛道|track)\s*[:：]\s*\*{0,2}\s*(theory|systems)\b", re.I
)

_REQUIRED_SECTIONS = {"问题阐述", "产出要求"}


def _strip_bold(text: str) -> str:
    return text.strip().strip("*").strip()


def parse_dead_format(md_text: str) -> Brief:
    """Parse a dead-format research brief. Raises :class:`DeadFormatError`
    when the structure is incomplete（调用方回退编译器）。"""
    if not md_text or not md_text.strip():
        raise DeadFormatError("empty document")

    lines = md_text.splitlines()

    # --- 标题：首个标题行 ---
    title = ""
    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            title = _strip_bold(m.group(1))
            break
    if not title:
        raise DeadFormatError("no markdown heading found（缺 # 标题）")

    # --- 轨道（元信息区，可缺省） ---
    track = "theory"
    for line in lines[:30]:
        m = _TRACK_RE.search(line)
        if m:
            track = m.group(1).lower()
            break

    # --- 单遍行式状态机 ---
    sections: list[tuple[str, list[str]]] = []      # (节名, 正文行)
    constraints: list[str] = []
    exclusions: list[str] = []

    mode: str | None = None      # None | "section" | "硬约束" | "明确不做"
    sec_name = ""
    buf: list[str] = []

    def _close() -> None:
        nonlocal buf
        if mode == "section" and sec_name:
            sections.append((sec_name, buf))
        buf = []

    for line in lines:
        s = line.strip()

        heading = _HEADING_RE.match(s) if s.startswith("#") else None
        if heading:
            text = _strip_bold(heading.group(1))
            sec_m = _SECTION_RE.match(text)
            sub_m = _SUB_RE.match(text)
            if sub_m:
                _close()
                mode = _SUB_ALIAS.get(sub_m.group(1), sub_m.group(1))
            elif sec_m:
                _close()
                mode, sec_name = "section", sec_m.group(2)
            else:
                # 节内小标题：并入节正文（保持原文），但打断硬约束列表
                if mode in ("硬约束", "明确不做"):
                    mode = None
                if mode == "section":
                    buf.append(line)
                else:
                    _close() if mode else None
                    mode = None
            continue

        sub_m = _SUB_RE.match(s)
        if sub_m:
            _close()
            mode = _SUB_ALIAS.get(sub_m.group(1), sub_m.group(1))
            continue

        if mode in ("硬约束", "明确不做"):
            b = _BULLET_RE.match(line)
            if b:
                item = b.group(1).strip()
                (constraints if mode == "硬约束" else exclusions).append(item)
            elif s:
                # 列表后的散 prose——跳出小节，忽略归节（避免误收）
                mode = None
            continue

        if mode == "section":
            buf.append(line.rstrip())
        # mode None（标题前元信息/小节间散文）不收

    _close()

    if not constraints:
        raise DeadFormatError("四、产出要求 下未找到『硬约束』bullet 清单")

    # 必有节按前缀匹配（节名常带尾注，如"产出要求（委托的交付物形态…）"）
    sec_names = {name for name, _ in sections}
    missing = {
        req for req in _REQUIRED_SECTIONS
        if not any(n.startswith(req) for n in sec_names)
    }
    if missing:
        raise DeadFormatError(f"缺少必有节：{sorted(missing)}")

    problem_area = "\n\n".join(
        f"## {name}\n" + "\n".join(body).strip()
        for name, body in sections
        if "\n".join(body).strip()
    )
    if not problem_area.strip():
        raise DeadFormatError("problem_area 为空（必有节无正文）")

    # --- knowledge_files：确定性路径扫描（与编译器共用实现） ---
    from haa.brief_compiler import scan_knowledge_paths

    knowledge_files = scan_knowledge_paths(md_text)

    logger.info(
        "dead-format parse ok: %d section(s), %d constraint(s), %d exclusion(s), "
        "%d knowledge file(s), track=%s",
        len(sections), len(constraints), len(exclusions),
        len(knowledge_files), track,
    )
    return Brief(
        title=title,
        problem_area=problem_area,
        constraints=constraints,
        exclusions=exclusions,
        track=track,
        knowledge_files=knowledge_files,
    )


def try_parse_dead_format(md_text: str) -> Brief | None:
    """None on DeadFormatError（方便 `load_brief` 的确定性优先/编译兜底）。"""
    try:
        return parse_dead_format(md_text)
    except DeadFormatError as exc:
        logger.info("dead-format parse fell back to compiler: %s", exc)
        return None
