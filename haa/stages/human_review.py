"""HUMAN_REVIEW — 人工审核关卡。

Part I（含实验设计）产出的论文前体成本极高，正式投入 Part II（真实实验 +
撰写）前必须由人工确认。本 stage 不调用 LLM：它仅把 campaign 状态置为
``AWAITING_HUMAN_REVIEW``，然后通过一个 ``paused=True`` 的返回值让 pipeline
的 ``_drive`` 循环暂停，等待外部 ``haa approve`` / ``haa reject`` CLI 命令继续
或终止。
"""

from __future__ import annotations

import logging

from haa.stages.base import BaseStage, StageResult, StageStatus

logger = logging.getLogger("haa.human_review")


class HumanReviewStage(BaseStage):
    """空 stage：不调用 LLM，返回 ``paused`` 标志让 pipeline 暂停。

    pipeline 在 ``_drive`` 中检测到 ``result.data["paused"]`` 后停止驱动循环
    （既不 retire 也不 publish），把控制权交还外部；人工通过
    ``haa approve`` 恢复（→ WRITE）或 ``haa reject`` 终止（→ RETIRED）。
    """

    name = "HUMAN_REVIEW"
    # 该阶段不调用 LLM，也不需要任何工具。
    allowed_tools: set[str] = set()

    def run(self, campaign, context):  # noqa: D401
        logger.info(
            "Campaign %s paused for human review. "
            "Use 'haa approve %s' to continue or 'haa reject %s' to retire.",
            campaign.id, campaign.id, campaign.id,
        )
        # 返回特殊标志：pipeline 检测到 paused=True 后停止 drive 循环。
        return StageResult(
            status=StageStatus.CONTINUE,
            data={"paused": True, "reason": "awaiting_human_review"},
            next_stage=None,
        )
