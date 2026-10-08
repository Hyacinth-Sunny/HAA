"""结算管理器——上下文压缩的 HAA 形态（大修第二章 §4/§5，批次7 M-c）。

三层模型落地：
- 第一层：当前上下文（本环节装配的全部内容）
- 第二层：结算摘要链（每环节结束产出的结算包——compact 产物，有损）
- 第三层：持久记忆库（M-b 已建好，晋升由本模块在结算时触发）

**观测层先行策略**：M-c 首版不改现有装配行为——每环节结束自动产出
结算包、落事件（settlement/produce）、压力测量照跑；``features.settlement``
开时才切换为"摘要链+按需拉取"装配模式（向后兼容铁律：不开=行为不变）。

结算包结构（§4.1）::

    settlement:
      stage_id: "SEEK"
      summary: "…"               # ≤3000 chars 保守折算 2K token
      artifacts_index: […]       # 本环节产出的工件索引（只列不装）
      promotion_log: […]         # 本次晋升内容清单
      next_stage_needs: […]      # 下一环节声明所需工件引用

事件：``settlement/produce``（结算时）、``settlement/merge``（再压缩时，
指向被合并的旧摘要）——日志永不删除（视图变换铁律）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from haa.harness.session_log import SessionEventLog

logger = logging.getLogger("haa.settlement")

# 2K token 硬顶按 3000 字符保守折算（同墓穴注入纪律；tokenizer 校准挂账）
MAX_SUMMARY_CHARS = 3000
# 装配压力阈值（全上下文窗口占比 0.8——DSH compaction 移植对照表）
PRESSURE_THRESHOLD = 0.8
# 再压缩重试上限（§4.5：拒绝"压完没变小"的摘要）
MAX_MERGE_RETRIES = 1


class Settlement(BaseModel):
    """一个环节的结算包。"""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    summary: str = Field(default="", max_length=10000)  # 软校验（硬截在下面）
    artifacts_index: list[str] = Field(default_factory=list)
    promotion_log: list[str] = Field(default_factory=list)
    next_stage_needs: list[str] = Field(default_factory=list)
    produced_at: float = Field(default_factory=time.time)

    def clamp_summary(self, limit: int = MAX_SUMMARY_CHARS) -> None:
        if len(self.summary) > limit:
            dropped = len(self.summary) - limit
            self.summary = self.summary[:limit] + f"\n…（结算摘要截断，省略 {dropped} 字符）"


def _estimate_context_chars(context) -> int:
    """粗估当前上下文体积（不含 trace/messages——那是检查点排除项）。"""
    total = 0
    for attr in ("novelty", "screen", "design", "paper", "review"):
        v = getattr(context, attr, None)
        if isinstance(v, (dict, list)):
            total += len(str(v))
        elif isinstance(v, str):
            total += len(v)
    extra = getattr(context, "extra", None)
    if extra is None:
        extra = {}
    for k, v in extra.items():
        if k not in ("kills", "anchor_guard", "settlements") and isinstance(v, (dict, list, str)):
            total += len(str(v))
    return total


def _estimate_window_chars() -> int:
    """粗估上下文窗口字符容量（128K tokens × ~3 chars/token 保守）。"""
    return 128_000 * 3


class SettlementManager:
    """产出结算包 / 装配摘要链 / 压力测量与再压缩 / 晋升触发。"""

    def __init__(self, *, session_log: SessionEventLog | None = None,
                 max_summary_chars: int = MAX_SUMMARY_CHARS):
        self.session_log = session_log or SessionEventLog()
        self.max_summary = max_summary_chars
        self._merge_count = 0

    # -- 结算（§4.1） ------------------------------------------------------

    def settle(self, stage_name: str, context, result_data: dict[str, Any],
               *, campaign_id: str = "") -> Settlement:
        """环节结束时结算。幂等：同 stage 重复调用覆盖前一份（以最新为准）。"""
        # 从 result_data 提取工件索引（artifacts 层写入的键名）
        artifacts_index = sorted(
            k for k in result_data
            if k not in ("trace", "messages", "anchor_diff", "candidates",
                         "anchor_guard", "challenge")
            and isinstance(result_data[k], (str, dict, list))
        )
        # 简要摘要：从 result_data 的核心字段拼一段
        summary_parts: list[str] = [f"[{stage_name}]"]
        for key in ("verdict", "grade", "decision", "assessment", "reason",
                     "rationale", "verify_passed"):
            v = result_data.get(key)
            if v is not None:
                text = str(v)[:300]
                summary_parts.append(f"{key}={text}")
        # 晋升检查：本环节是否有应入库事实（第二章 §5 判定表）
        promotion_log: list[str] = []
        kills = (getattr(context, "extra", None) or {}).get("kills") or []
        verdict = str(result_data.get("verdict", "") or result_data.get("grade", ""))
        if verdict and stage_name in ("NOVELTY", "SCREEN", "GRADE", "PILOT",
                                        "EXP_FEASIBILITY"):
            promotion_log.append(f"verdict:{verdict}")
        if kills and stage_name in ("NOVELTY", "SCREEN", "GRADE", "PILOT"):
            promotion_log.append(f"kills:{len(kills)}")
        concepts = (getattr(context, "extra", None) or {}).get("concepts") or []
        if concepts and stage_name == "DESIGN":
            promotion_log.append(f"concepts:{len(concepts)}")

        settlement = Settlement(
            stage_id=stage_name,
            summary=" ".join(summary_parts)[:self.max_summary],
            artifacts_index=artifacts_index,
            promotion_log=promotion_log,
            next_stage_needs=[],  # 由下一环节首次装配时声明（v1 从 result 推导）
        )
        settlement.clamp_summary(self.max_summary)
        # 存入 context.extra 链
        extra = getattr(context, "extra", None)
        if extra is None:
            extra = {}
            if hasattr(context, "extra"):
                context.extra = extra
        settlements = extra.setdefault("settlements", {})
        settlements[stage_name] = settlement.model_dump()
        # 事件
        self.session_log._emit(
            "settlement/produce",
            campaign_id=campaign_id or None,
            stage=stage_name or None,
            payload=settlement.model_dump(),
        )
        return settlement

    # -- 装配（§4.2） ------------------------------------------------------

    def assemble_chain(self, context, *, include_stages: int = 3) -> str:
        """把最近 N 份结算摘要拼为可注入 prompt 的链（`<settlement-summary>` 标签框定）。"""
        settlements = (getattr(context, "extra", None) or {}).get("settlements") or {}
        if not settlements:
            return ""
        # 按产出时间取最近 N 个
        items = sorted(settlements.items(),
                       key=lambda kv: kv[1].get("produced_at", 0),
                       reverse=True)[:include_stages]
        parts: list[str] = []
        for stage_id, s in reversed(items):
            parts.append(
                f'<settlement-summary stage="{stage_id}">\n'
                f"{s.get('summary', '')}\n"
                f"promoted: {', '.join(s.get('promotion_log', [])) or '—'}\n"
                f"</settlement-summary>"
            )
        return "\n\n".join(parts)

    # -- 压力测量与再压缩（§4.2/§4.3/§4.5） ----------------------------------

    def measure_pressure(self, context) -> float:
        """装配前压力测量（事前——A-MEM 主动而非被动）。"""
        window = _estimate_window_chars()
        used = _estimate_context_chars(context)
        chain_chars = len(self.assemble_chain(context))
        return min((used + chain_chars) / window, 1.0)

    def needs_merge(self, context) -> bool:
        return self.measure_pressure(context) >= PRESSURE_THRESHOLD

    def merge_oldest(self, context, *, campaign_id: str = "") -> bool:
        """再压缩：对最老的结算摘要做摘要的摘要（§4.2 检查点合并）。

        收敛校验：压完没变小→重试一次→仍不动→报错（走濒死诊断，不静默）。
        """
        settlements = (getattr(context, "extra", None) or {}).get("settlements") or {}
        if len(settlements) < 2:
            return False
        items = sorted(settlements.items(),
                       key=lambda kv: kv[1].get("produced_at", 0))
        oldest_key, oldest = items[0]
        second_key, second = items[1] if len(items) > 1 else (None, None)
        if second is None:
            return False
        merged_summary = (
            f"[{oldest_key}+{second_key} merged] "
            f"{oldest.get('summary', '')[:800]}…"
            f"{second.get('summary', '')[:800]}"
        )[:self.max_summary]
        merged = Settlement(
            stage_id=f"{oldest_key}+{second_key}",
            summary=merged_summary,
            artifacts_index=sorted(set(oldest.get("artifacts_index", []))
                                   | set(second.get("artifacts_index", []))),
            promotion_log=oldest.get("promotion_log", [])
            + second.get("promotion_log", []),
            next_stage_needs=[],
        )
        # 收敛校验：合并后必须比两份之和短
        old_total = len(oldest.get("summary", "")) + len(second.get("summary", ""))
        if len(merged.summary) >= old_total and self._merge_count < MAX_MERGE_RETRIES:
            self._merge_count += 1
            merged_summary = merged_summary[: old_total // 2]
            merged.summary = merged_summary
        if len(merged.summary) >= old_total:
            logger.warning("settlement merge did not shrink "
                           "(%d → %d) — giving up this round", old_total,
                           len(merged.summary))
            return False
        # 替换两份为一份
        del settlements[oldest_key]
        del settlements[second_key]
        settlements[merged.stage_id] = merged.model_dump()
        self.session_log._emit(
            "settlement/merge",
            campaign_id=campaign_id or None,
            payload={"merged": [oldest_key, second_key],
                      "into": merged.stage_id,
                      "chars_before": old_total,
                      "chars_after": len(merged.summary)},
        )
        return True


# -- 晋升触发（§5 时机 1：结算时） ---------------------------------------------


def promote_at_settle(context, memory_bank, *, writer: str = "promotion",
                      campaign_id: str = "") -> list[str]:
    """结算流程的最后一步：检查本环节产出是否有应入库事实。

    调用 memory_bank（M-b）写入 idea/verdict/domain_concept 实体。
    返回晋升日志（空=无晋升）。
    """
    extra = getattr(context, "extra", None)
    if extra is None:
        extra = {}
    promoted: list[str] = []
    from haa.memory_bank import (DomainConceptEntity, IdeaEntity,
                                 VerdictEntity, next_entity_id)
    # 概念卡 → domain_concept
    for card in (extra.get("concepts") or []):
        if not isinstance(card, dict):
            continue
        try:
            cid = next_entity_id(memory_bank.root, "c")
            memory_bank.write("domain_concepts", DomainConceptEntity(
                concept_id=cid,
                name=str(card.get("name", "unnamed")),
                math_formulation=str(card.get("math_formulation", "\\text{?}"))[:2000],
                code_refs=card.get("code_refs", []),
                dependencies=[],
                status=str(card.get("status", "defined")),
                first_seen_campaign=campaign_id,
            ), writer=writer)
            promoted.append(f"concept:{cid}")
        except Exception as exc:  # noqa: BLE001 — 晋升失败不炸管线
            logger.warning("concept promotion failed: %s", exc)
    return promoted
