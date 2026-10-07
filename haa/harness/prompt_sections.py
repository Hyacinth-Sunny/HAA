"""提示词注册表与楼层编号（大修计划书第一章 §3.3）。

任何模块（harness 自身、各 stage、各工具、未来的记忆模块）都可向注册表
**投稿**一个提示词段落并附带楼层编号；每次调用模型前，组装点把全部段落
按楼层升序拼接成系统提示词。

楼层规划表（编号区间既定，内容随施工充实）::

    -100      HAA 身份声明（harness 固定）
    -50       部署环境说明（本机路径、可用资源）
    0         用户 persona＋研究简报锚定（简报编译器投稿）
    50~99     记忆注入区（动态内容；第二章，M0 留空）
    100~199   工具跨调用守则（每个工具一两句，工具自己投稿）
    200+      各阶段专属指令（各 stage 投稿）

三条硬规则：
1. 楼层内按登记顺序；
2. **工具菜单不进系统提示词**——随消息组装动态生成（按 stage 过滤），
   本模块根本不接受工具清单参数，物理上不可能违反；
3. **逃生舱**：声明 ``complete=True`` 的段落独占整个系统提示词
   （供未来的专用子代理自带完整人设）；工具清单照常由 registry 组装。

M0 现状：``haa/prompts.py`` 的静态 Jinja2 模板与 ``BaseStage._brief_block``
字符串拼接仍按原路工作（兼容期）；各 stage 向楼层 200+ 的投稿随 P1 各批次
迁移，迁移完成前本注册表主要承载 harness 固定层与工具守则层。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("haa.harness.prompt_sections")

FLOOR_IDENTITY = -100
FLOOR_ENVIRONMENT = -50
FLOOR_BRIEF_ANCHOR = 0
FLOOR_MEMORY = (50, 99)      # 区间：记忆注入区（第二章专用）
FLOOR_TOOL_RULES = (100, 199)
FLOOR_STAGE = 200            # 200+：阶段专属

RenderFn = Callable[[dict[str, Any]], str]


@dataclass
class Section:
    """一个提示词段落投稿。

    ``text`` 静态文本；``render`` 动态渲染（每次组装时以调用上下文为参）。
    ``complete=True`` = 逃生舱（独占整个系统提示词；同时存在多个时按
    楼层序取最先登记者并告警）。
    """

    source: str            # 投稿者标识（模块名/stage 名）
    floor: int             # 楼层号
    text: str = ""
    render: RenderFn | None = None
    complete: bool = False
    seq: int = 0           # 同楼层内的登记序（自动分配）

    def render_text(self, context: dict[str, Any]) -> str:
        if self.render is not None:
            try:
                return self.render(context or {})
            except Exception:  # noqa: BLE001 — 动态段渲染失败不炸组装
                logger.exception("prompt section render failed: %s", self.source)
                return ""
        return self.text


class PromptSectionRegistry:
    """段落登记 + 按楼层拼装。"""

    def __init__(self) -> None:
        self._sections: list[Section] = []
        self._seq = 0

    def register(
        self,
        *,
        source: str,
        floor: int,
        text: str = "",
        render: RenderFn | None = None,
        complete: bool = False,
    ) -> Section:
        self._seq += 1
        sec = Section(
            source=source, floor=floor, text=text, render=render,
            complete=complete, seq=self._seq,
        )
        self._sections.append(sec)
        return sec

    def unregister(self, source: str, floor: int | None = None) -> None:
        self._sections = [
            s for s in self._sections
            if not (s.source == source and (floor is None or s.floor == floor))
        ]

    def sections(self) -> list[Section]:
        """按（楼层, 登记序）排序的全部段落。"""
        return sorted(self._sections, key=lambda s: (s.floor, s.seq))

    def assemble(self, context: dict[str, Any] | None = None) -> str:
        """拼装系统提示词。逃生舱规则：complete 段独占。"""
        ctx = context or {}
        completes = [s for s in self.sections() if s.complete]
        if completes:
            if len(completes) > 1:
                logger.warning(
                    "multiple complete=True sections: %s — using first by floor",
                    [s.source for s in completes],
                )
            return completes[0].render_text(ctx).strip()
        parts: list[str] = []
        for sec in self.sections():
            txt = sec.render_text(ctx).strip()
            if txt:
                parts.append(txt)
        return "\n\n".join(parts)

    # -- 楼层合法性 ---------------------------------------------------------

    @staticmethod
    def floor_allowed(floor: int) -> bool:
        return floor != FLOOR_BRIEF_ANCHOR or True  # 全楼层开放；区间语义由投稿者遵守


def default_identity_section() -> Section:
    """楼层 -100 的 HAA 身份声明（harness 固定投稿）。"""
    return Section(
        source="harness.identity",
        floor=FLOOR_IDENTITY,
        text=(
            "你是运行在 HAA（Hyacinth Automated Analyzer）中的研究 agent。"
            "HAA 是一条自动科研流水线：你当前在一个被明确命名的阶段内工作，"
            "阶段目标以任务提示给出；你不知道也不需要知道整条流水线的全局进度。"
            "调用工具时遵守各工具结果中的纪律提示（如退出码标记、截断说明）。"
        ),
    )
