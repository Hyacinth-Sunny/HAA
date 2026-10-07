"""Research Brief — the human-authored input that seeds a campaign.

A Brief is an immutable input document: a person writes one describing the
problem they want explored, drops it in the queue, and HAA turns it into a
campaign. Two briefs with identical *content* must hash to the same value so
that duplicate campaigns can be detected.

HM-Pro Lesson 6 (structured output is strict): every field is explicitly
declared and ``extra="forbid"`` rejects undeclared keys, so a schema mismatch
fails loudly instead of silently dropping data.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Track(str, Enum):
    """Research track.

    * THEORY — proofs, bounds, impossibility results.
    * SYSTEMS — algorithms, implementations, measurements.
    """

    THEORY = "theory"
    SYSTEMS = "systems"




class HypothesisAnchor(BaseModel):
    """锚定模式入口（大修第三章 §3）：存在即锚定，缺失即开放模式。

    锚点是待检验的对象，不是被复制的目标——防脑裂三件套（偏差说明/
    保真检查器/异议上报）围绕它工作。提交时对规范化序列化计算
    SHA-256 前 8 位作为 idea-ID（锚点从此不可变，后续所有环节引用）。
    """

    model_config = ConfigDict(extra="forbid")

    core_claim: str = Field(..., min_length=1,
                            description="核心主张（一句话）")
    expected_mechanism: str = Field(..., min_length=1,
                                    description="预期机制：为什么这个主张成立")
    success_criteria: str = Field(..., min_length=1,
                                  description="成功判据：什么现象出现算验证成功")
    confidence: str = Field(..., description="high | medium | low 置信度声明")

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("high", "medium", "low"):
            raise ValueError("confidence must be high|medium|low")
        return v


class Brief(BaseModel):
    """A research brief written by a human.

    All fields are explicit (Lesson 6). ``constraints``/``exclusions`` default
    to empty so a minimal brief (title + problem_area) is still valid.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, description="Working title / theme.")
    problem_area: str = Field(
        ..., min_length=1, description="The research problem or domain to explore."
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Hard requirements the resulting work must satisfy.",
    )
    exclusions: list[str] = Field(
        default_factory=list,
        description=(
            "What to avoid. HM-Pro philosophy 5: close the browser and think first "
            "so we don't drift into incremental variants of existing papers' future work."
        ),
    )
    track: Track = Field(
        default=Track.THEORY,
        description="theory (proofs/bounds) or systems (algorithms/measurements).",
    )
    skip_to: str | None = Field(
        default=None,
        description="跳转到指定阶段（旁路模式）。如 'exp_spec' 跳过 SEEK→…→GRADE 直接进 EXP_SPEC。",
    )
    hypothesis_anchor: HypothesisAnchor | None = Field(
        default=None,
        description="锚定模式入口（第三章 §3）：存在即锚定、缺失即开放模式。",
    )
    knowledge_files: list[str] = Field(
        default_factory=list,
        description=(
            "支撑材料文件（绝对路径或 project_root 相对路径）。campaign 启动时"
            "复制进 campaigns/<cid>/knowledge/ 供 read_file 按需读取（沙箱="
            "campaign 目录）。如 16 个研究包笔记。"
        ),
    )

    @property
    def anchor_idea_id(self) -> str | None:
        """锚点规范化哈希的 idea-ID（a+sha256 前 8 位；无锚点为 None）。"""
        if self.hypothesis_anchor is None:
            return None
        from haa.memory_bank import idea_id_from_anchor

        a = self.hypothesis_anchor
        return idea_id_from_anchor(a.core_claim, a.expected_mechanism,
                                   a.success_criteria)

    @field_validator("constraints", "exclusions", "knowledge_files")
    @classmethod
    def _drop_blank_entries(cls, v: list[str]) -> list[str]:
        """Trim and discard empty constraint/exclusion strings."""
        cleaned = [s.strip() for s in v]
        return [s for s in cleaned if s]

    def brief_hash(self) -> str:
        """Stable SHA-256 over the canonical content of the brief.

        Used to dedup campaigns that target the same brief — two briefs that
        differ only in list order or whitespace hash identically.
        knowledge_files 参与哈希：知识变了简报语义就变（v1.0.2）。
        skip_to 是流程旁路标记，不参与（沿用既有语义）。
        """
        payload = {
            "title": self.title.strip(),
            "problem_area": self.problem_area.strip(),
            "constraints": sorted(self.constraints),
            "exclusions": sorted(self.exclusions),
            "track": self.track.value,
            "knowledge_files": sorted(self.knowledge_files),
        }
        # sort_keys + ensure_ascii=False => deterministic across runs/locales.
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
