"""Pipeline — the code-driven state machine that drives stage execution.

This is HAA's core (philosophy 1: *the model doesn't know which step it's on;
code does*). A stage returns a :class:`~haa.stages.base.StageResult` (a status +
findings); the transition rules here resolve the next stage. A stage **never**
decides the next stage on its own authority.

State graph
-----------
::

    SEEK → NOVELTY → SCREEN → DESIGN ⇄ VERIFY → GRADE
        │       │        │                 │
        │       └─(dead)─┴─────(dead)──────┴──→ advance queue (Lesson 4)
        │                                            queue empty → RETIRED
        └─ GRADE: TRIVIAL/LOOPHOLE → advance queue
           GRADE: SOLID/THIN     → WRITE → REVIEW
                                         ├ accept → PUBLISHED
                                         └ reject → REFINE → REVIEW (≤ max_review_rounds)
                                                    exceed → PUBLISHED (degraded)

HM-Pro lessons encoded here
---------------------------
* **Lesson 1** — VERIFY→DESIGN rework carries ``verify_findings`` into the
  context (see :meth:`Pipeline._after_verify`).
* **Lesson 2** — VERIFY's PASS is "no counterexample found"; the loop is capped,
  so an incomplete verify proceeds to GRADE instead of blocking forever.
* **Lesson 3** — after REVIEW the best snapshot is saved; REFINE rolls back to
  it if a round regressed (see :meth:`Pipeline._maybe_save_best_snapshot`).
* **Lesson 4** — a dead candidate is archived and the queue advances; only an
  *empty* queue retires the campaign.
* **Lesson 5** — only gated stages (SEEK/SCREEN, DESIGN/VERIFY, REVIEW/REFINE)
  can trip the budget gate; GRADE/WRITE never do. ``BudgetExhausted`` retires.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any

from haa.budget import BudgetExhausted, BudgetManager
from haa.config import Config
from haa.llm import LLMClient
from haa.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    GradeVerdict,
)
from haa.state import StateStore
from haa.stages import (
    BaseStage,
    DesignStage,
    ExpFeasibilityStage,
    PilotStage,
    AnalyzeStage,
    ExpSpecStage,
    GradeStage,
    HumanReviewStage,
    NoveltyStage,
    RefineStage,
    ReviewStage,
    ScreenStage,
    SeekStage,
    StageContext,
    StageResult,
    StageStatus,
    VerifyStage,
    WriteStage,
)

logger = logging.getLogger("haa.pipeline")

# Hard backstop on iterations so a runaway loop can never hang the pipeline.
_MAX_ITERATIONS = 1000


class StageName(str, Enum):
    """The discrete stages the pipeline can run.

    Values mirror the stage names used by the budget gate
    (``is_gated_stage``) and the LLM client's ``stage=`` tag.
    """

    SEEK = "SEEK"
    NOVELTY = "NOVELTY"
    SCREEN = "SCREEN"
    DESIGN = "DESIGN"
    VERIFY = "VERIFY"
    GRADE = "GRADE"
    WRITE = "WRITE"
    REVIEW = "REVIEW"
    REFINE = "REFINE"
    EXP_SPEC = "EXP_SPEC"                # 实验规格设计
    EXP_FEASIBILITY = "EXP_FEASIBILITY"  # 实验可行性检验
    PILOT = "PILOT"
    ANALYZE = "ANALYZE"                # P2 实验结果分析+观点终审（修订§3.1，批次18）                      # 先导实验微阶段（M2/第三章 §8.2，特性开关 pilot）
    HUMAN_REVIEW = "HUMAN_REVIEW"        # 人工审核关卡（不调用 LLM，暂停 pipeline）


# Re-export: the task brief calls the inter-stage carrier ``CampaignContext``;
# it is the same concept as StageContext (see haa/stages/base.py).
CampaignContext = StageContext

# Stage ↔ Campaign status (-ING values) translation.
_STAGE_TO_STATUS: dict[StageName, CampaignStatus] = {
    StageName.SEEK: CampaignStatus.SEEKING,
    StageName.NOVELTY: CampaignStatus.NOVELTY_CHECK,
    StageName.SCREEN: CampaignStatus.SCREENING,
    StageName.DESIGN: CampaignStatus.DESIGNING,
    StageName.VERIFY: CampaignStatus.VERIFYING,
    StageName.GRADE: CampaignStatus.GRADING,
    StageName.WRITE: CampaignStatus.WRITING,
    StageName.REVIEW: CampaignStatus.REVIEWING,
    StageName.REFINE: CampaignStatus.REFINING,
    StageName.EXP_SPEC: CampaignStatus.EXP_SPECIFYING,
    StageName.EXP_FEASIBILITY: CampaignStatus.EXP_CHECKING,
    StageName.PILOT: CampaignStatus.EXP_CHECKING,
    StageName.ANALYZE: CampaignStatus.WRITING,  # 复用写作中态（分析属产出层）  # 复用可行性检验态（先导属实验检验族）
    StageName.HUMAN_REVIEW: CampaignStatus.AWAITING_HUMAN_REVIEW,
}
_STATUS_TO_STAGE: dict[CampaignStatus, StageName] = {
    v: k for k, v in _STAGE_TO_STATUS.items()
}


def _make_settlement_log(pipeline):
    """结算事件日志：与主管线共享 sink（如有）。"""
    from haa.harness.session_log import SessionEventLog, _RecordingSink
    llm = getattr(pipeline, "llm", None)
    sink = getattr(llm, "event_sink", None) or _RecordingSink()
    return SessionEventLog(sink)


class Pipeline:
    """Drive stages to completion for one campaign.

    Parameters
    ----------
    config:
        Loaded :class:`~haa.config.Config`.
    state_store:
        :class:`~haa.state.StateStore` for persistence + checkpoints.
    budget_manager:
        :class:`~haa.budget.BudgetManager`.
    llm:
        Optional :class:`~haa.llm.LLMClient`; built from config if omitted.
    stages:
        Optional ``{StageName: BaseStage}`` mapping — injected by tests. When
        omitted, the default nine stages are constructed around ``llm``.
    """

    def __init__(
        self,
        config: Config,
        state_store: StateStore,
        budget_manager: BudgetManager,
        *,
        llm: LLMClient | Any | None = None,
        stages: dict[StageName, BaseStage] | None = None,
    ) -> None:
        self.config = config
        self.store = state_store
        self.budget = budget_manager
        self.llm = llm if llm is not None else self._build_llm()
        self.stages = stages if stages is not None else self._default_stages()
        # Stage names executed during the current run (debugging / tests).
        self.trace: list[str] = []
        # 启动 banner（smoke4 教训：想设 $5 实际跑了 $100——启动时不打印
        # 生效配置，错配无从事前发现）。只在真配置上打（测试注入不打）。
        if llm is None or stages is None:
            from haa.config import resolve_config_path

            logger.info(
                "HAA pipeline config | source=%s model=%s sub_model=%s "
                "provider_opts=%s "
                "global_limit=$%.2f per_campaign=$%.2f "
                "timeouts(llm/idle=%ss wall=%ss stage=%ss) "
                "seek=%d output=%d",
                resolve_config_path(),
                config.llm.model,
                config.llm.sub_agent_model or "-",
                config.llm.provider_options or "-",
                config.budget.global_limit,
                config.budget.per_campaign,
                config.timeouts.llm,
                config.timeouts.llm_wall,
                config.timeouts.stage,
                config.pipeline.seek_candidate_count,
                config.pipeline.output_candidate_count,
            )

    # -- construction helpers ---------------------------------------------
    def _build_llm(self) -> LLMClient:
        c = self.config.llm
        api_key = os.environ.get(c.api_key_env) or None
        # Inject the SQLiteEventSink so every LLM call's cost/tokens/duration
        # is persisted to the events table (Phase 6a-fronted observability).
        from haa.observability import SQLiteEventSink

        return LLMClient(
            model=c.model,
            timeout=self.config.timeouts.llm,
            wall_timeout=self.config.timeouts.llm_wall,
            max_retries=c.max_retries,
            api_base=c.api_base or None,
            api_key=api_key,
            provider_options=c.provider_options,
            pricing=c.pricing,
            budget=self.budget,
            event_sink=SQLiteEventSink(self.store),
        )

    def _default_stages(self) -> dict[StageName, BaseStage]:
        return {
            StageName.SEEK: SeekStage(self.llm, self.config),
            StageName.NOVELTY: NoveltyStage(self.llm, self.config),
            StageName.SCREEN: ScreenStage(self.llm, self.config),
            StageName.DESIGN: DesignStage(self.llm, self.config),
            StageName.VERIFY: VerifyStage(self.llm, self.config),
            StageName.GRADE: GradeStage(self.llm, self.config),
            StageName.WRITE: WriteStage(self.llm, self.config),
            StageName.REVIEW: ReviewStage(self.llm, self.config),
            StageName.REFINE: RefineStage(self.llm, self.config),
            StageName.EXP_SPEC: ExpSpecStage(self.llm, self.config),
            StageName.EXP_FEASIBILITY: ExpFeasibilityStage(self.llm, self.config),
            StageName.PILOT: PilotStage(self.llm, self.config),
            StageName.ANALYZE: AnalyzeStage(self.llm, self.config),
            StageName.HUMAN_REVIEW: HumanReviewStage(self.llm, self.config),
        }

    # -- public API --------------------------------------------------------
    def run_campaign(
        self,
        campaign_id: str,
        *,
        brief: Any | None = None,
    ) -> Campaign:
        """Run (or resume) a campaign to a terminal state.

        ``brief`` seeds a fresh campaign; on resume it is recovered from the
        latest checkpoint. Returns the final (PUBLISHED / RETIRED) Campaign.
        """
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {campaign_id}")

        snap = self.store.restore(campaign_id)
        context, stage = self._resume(campaign, snap, brief)
        self.trace = []
        # 知识文件铺设（v1.0.2）：fresh 启动 + brief 带知识 → 复制进 campaign
        # 沙箱的 knowledge/，read_file 即刻可读（幂等：已存在跳过）。
        if brief is not None and getattr(brief, "knowledge_files", None):
            self._stage_knowledge_files(campaign_id, brief)
        return self._drive(campaign, context, stage)

    def _stage_knowledge_files(self, campaign_id: str, brief: Any) -> None:
        """Copy brief.knowledge_files into campaigns/<cid>/knowledge/."""
        import shutil

        try:
            base = self.config.storage.resolved_campaigns_dir()
            dest_dir = Path(base) / campaign_id / "knowledge"
            dest_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for raw in brief.knowledge_files:
                src = Path(raw.strip())
                if not src.is_absolute():
                    src = self.config.project_root / src
                if not src.is_file():
                    logger.warning("knowledge file missing, skipped: %s", raw)
                    continue
                dest = dest_dir / src.name
                if not dest.exists():
                    shutil.copy2(src, dest)
                    copied += 1
            if copied:
                logger.info(
                    "staged %d knowledge file(s) into campaigns/%s/knowledge/",
                    copied, campaign_id,
                )
        except Exception as exc:  # noqa: BLE001 — 知识层绝不阻断主管线
            logger.warning("stage_knowledge_files failed: %s", exc)

    # -- resume ------------------------------------------------------------
    def _resume(self, campaign, snap, brief):
        """Rebuild the StageContext and pick the stage to (re)start from."""
        if snap is not None and snap.has_checkpoint:
            context = StageContext.from_checkpoint(snap.checkpoint.context)
            if brief is not None and context.brief is None:
                context.brief = brief
        else:
            context = StageContext(brief=brief)

        # Fall back to the persisted queue if the checkpoint lacked it.
        if snap is not None and not context.candidates:
            context.candidates = list(snap.candidates)
        if (
            snap is not None
            and context.candidate is None
            and campaign.current_candidate_id
        ):
            cand = next(
                (c for c in snap.candidates if c.id == campaign.current_candidate_id),
                None,
            )
            if cand is not None:
                context.candidate = cand
                context.current_candidate_id = cand.id

        if campaign.is_terminal:
            stage = None
        elif campaign.status == CampaignStatus.AWAITING_HUMAN_REVIEW:
            # 人工审核通过（haa approve）后从 WRITE 继续。
            # R2 批次18-1：features.analyze 开时经 ANALYZE（生产入口）。
            if self.config is not None and self.config.harness.feature("analyze"):
                stage = StageName.ANALYZE
            else:
                stage = StageName.WRITE
        elif campaign.status in _STATUS_TO_STAGE:
            stage = _STATUS_TO_STAGE[campaign.status]
        else:  # QUEUED (or any non -ING state) → start at SEEK.
            stage = StageName.SEEK
        # 旁路模式：brief.skip_to 直接跳到指定阶段，跳过前面的阶段
        brief_skip_to = getattr(brief, "skip_to", None) if brief else None
        if brief_skip_to == "exp_spec":
            stage = StageName.EXP_SPEC
        elif brief_skip_to == "novelty":
            # Worker Campaign（P1 批量）：候选已通过 store.save_candidate() 预置。
            # _resume 上方已从 snap.candidates 加载它；这里显式激活（跳过 SEEK
            # 意味着 _after_seek 的 _ensure_active_candidate 不会跑）。
            stage = StageName.NOVELTY
            self._ensure_active_candidate(campaign, context)
            if context.candidate is None:
                logger.warning(
                    "skip_to=novelty but no pre-seeded candidate for campaign %s — retiring",
                    campaign.id,
                )
                self._retire(campaign, context, reason="skip_to_novelty:empty_queue")
                stage = None
        return context, stage

    # -- main loop ---------------------------------------------------------
    def _drive(self, campaign, context, stage) -> Campaign:
        iteration = 0
        while stage is not None and not campaign.is_terminal:
            iteration += 1
            if iteration > _MAX_ITERATIONS:
                logger.error("pipeline iteration cap hit — retiring")
                self._retire(campaign, context, reason="iteration_cap")
                break

            self.trace.append(stage.value)
            stage_obj = self.stages[stage]

            # Pre-stage checkpoint: persist that we are entering this stage.
            campaign.status = _STAGE_TO_STATUS[stage]
            self._persist(campaign, context, f"{stage.value}:start")

            # Run the stage. Budget exhaustion on a gated stage retires
            # gracefully (Lesson 5); other errors surface as real failures.
            try:
                result = stage_obj.run(campaign, context)
            except BudgetExhausted as exc:
                self._retire(campaign, context, reason=f"budget:{exc.scope}")
                break

            # Human gate：检测到暂停标志后停止 drive（不 retire、不 publish），
            # 把控制权交还外部（haa approve / reject）。等待人工审核后再 resume。
            if result.data.get("paused"):
                campaign.status = CampaignStatus.AWAITING_HUMAN_REVIEW
                self._persist(campaign, context, "HUMAN_REVIEW:paused")
                return campaign

            # 大修批次4（P1-a）：锚定模式三件套逐环节检查——偏差说明解析、
            # 保真评分（报警阈值 7/10）、异议报告捕获。challenge 非空 →
            # 管线暂停交用户裁决（复用 paused 通道；v1 呈现走事件日志与
            # checkpoint 的 extra，Web 专属视图在前端批次补）。
            anchor = getattr(getattr(context, "brief", None), "hypothesis_anchor", None)
            if anchor is not None and (
                self.config is None
                or self.config.harness.feature("anchored_mode")
            ):
                from haa.anchor_guard import check_stage_output

                guard = check_stage_output(anchor, result.data or {})
                context.extra.setdefault("anchor_guard", {})[stage.value] = {
                    k: v for k, v in guard.items()}
                if guard["fidelity"] and guard["fidelity"]["alarm"]:
                    logger.warning(
                        "anchor fidelity alarm at %s: %s/10 — output may have drifted "
                        "from the anchor", stage.value, guard["fidelity"]["total"],
                    )
                if guard["challenge"]:
                    logger.warning(
                        "anchor challenge raised at %s: %s — pausing for user "
                        "arbitration", stage.value, guard["challenge"]["challenge_type"],
                    )
                    context.extra["pending_challenge"] = {
                        "stage": stage.value, **guard["challenge"]}
                    campaign.status = CampaignStatus.AWAITING_HUMAN_REVIEW
                    self._persist(campaign, context, "ANCHOR_CHALLENGE:paused")
                    return campaign

            # Observability hook (B3): surface truncation + per-stage measures.
            self._observe(stage_obj, stage.value, campaign)

            # Resolve the next stage (folds result into context; may go terminal).
            next_stage = self._transition(stage, result, campaign, context)
            if campaign.is_terminal:
                break

            # M-c 结算观测层（feature settlement 开时启用；关=零行为变化）
            if self.config.harness.feature("settlement"):
                from haa.settlement import SettlementManager
                sm = SettlementManager(
                    session_log=getattr(self, "_settlement_log", None) or
                    _make_settlement_log(self))
                sm.settle(stage.value, context, result.data or {},
                          campaign_id=campaign.id)
                # P1-4 心跳：逐 stage 打 pressure（区分"未达阈"vs"坏了"）
                _pressure = sm.measure_pressure(context)
                logger.info("settlement heartbeat: stage=%s pressure=%.3f "
                            "(threshold=%.2f) settlements=%d",
                            stage.value, _pressure, 0.8,
                            len((context.extra or {}).get("settlements", {})))
                if sm.needs_merge(context):
                    sm.merge_oldest(context, campaign_id=campaign.id)
                self._settlement_log = sm.session_log

            # Post-stage checkpoint: persist the folded context.
            self._persist(campaign, context, f"{stage.value}:done")
            stage = next_stage

        return campaign

    # -- observability (B3) ------------------------------------------------
    def _observe(self, stage_obj, stage_name, campaign) -> None:
        """Surface per-stage observability signals from ``stage_obj._last_meta``.

        ``_run_agent`` (BaseStage) sets ``_last_meta`` after each agent loop;
        we read it here (not from ``result.data``) so stages don't have to touch
        their return values. Emits ``stage_truncated`` + a warning when a stage
        hit its tool-call cap, and a ``stage_measure`` event for tuning data.
        """
        meta = getattr(stage_obj, "_last_meta", None)
        if not meta:
            return
        iterations = int(meta.get("iterations", 0))
        tool_calls_count = int(meta.get("tool_calls_count", 0))
        cost = float(meta.get("total_cost_usd", 0.0))
        if meta.get("truncated"):
            logger.warning(
                "stage %s hit max_tool_calls (truncated) in campaign %s "
                "(iterations=%d, tool_calls=%d)",
                stage_name, campaign.id, iterations, tool_calls_count,
            )
            self._emit(
                campaign_id=campaign.id, stage=stage_name,
                event_type="stage_truncated",
                payload={"iterations": iterations, "tool_calls_count": tool_calls_count},
                cost_usd=cost,
            )
        self._emit(
            campaign_id=campaign.id, stage=stage_name,
            event_type="stage_measure",
            payload={"iterations": iterations, "tool_calls_count": tool_calls_count},
            cost_usd=cost,
        )

    def _emit(
        self, *, campaign_id, stage, event_type, payload=None,
        cost_usd=0.0, tokens=0, duration_s=0.0,
    ) -> None:
        """Persist an event; never raises (observability must not crash runs)."""
        try:
            self.store.save_event(
                event_type=event_type, campaign_id=campaign_id, stage=stage,
                payload=payload, cost_usd=cost_usd, tokens=tokens, duration_s=duration_s,
            )
        except Exception:
            pass

    # -- transition dispatch ----------------------------------------------
    def _transition(self, stage, result, campaign, context) -> StageName | None:
        """The state-transition table. Returns the next stage or None (terminal)."""
        # ABORT_CAMPAIGN from any stage → retire the whole campaign.
        if result.status == StageStatus.ABORT_CAMPAIGN:
            self._retire(campaign, context, reason=result.data.get("reason", "abort_campaign"))
            return None

        if stage == StageName.SEEK:
            return self._after_seek(result, campaign, context)
        if stage == StageName.NOVELTY:
            if result.status == StageStatus.ABORT_CANDIDATE:
                self._record_kill("NOVELTY", context, result)
                return self._advance_or_retire(campaign, context, result.data.get("reason", ""))
            context.novelty = result.data
            return StageName.SCREEN
        if stage == StageName.SCREEN:
            if result.status == StageStatus.ABORT_CANDIDATE:
                self._record_kill("SCREEN", context, result)
                return self._advance_or_retire(campaign, context, result.data.get("reason", ""))
            context.screen = result.data
            return StageName.DESIGN
        if stage == StageName.DESIGN:
            context.design = result.data
            return StageName.VERIFY
        if stage == StageName.VERIFY:
            return self._after_verify(result, campaign, context)
        if stage == StageName.GRADE:
            return self._after_grade(result, campaign, context)
        if stage == StageName.WRITE:
            context.paper = result.data
            self._validate_paper(context, campaign, "WRITE")  # R6 校验在回写后
            return StageName.REVIEW
        if stage == StageName.REVIEW:
            return self._after_review(result, campaign, context)
        if stage == StageName.REFINE:
            context.paper = result.data
            self._validate_paper(context, campaign, "REFINE")  # R6 校验新稿
            return StageName.REVIEW
        if stage == StageName.EXP_SPEC:
            context.extra["exp_spec"] = result.data
            return StageName.EXP_FEASIBILITY
        if stage == StageName.EXP_FEASIBILITY:
            return self._after_exp_feasibility(result, campaign, context)
        if stage == StageName.PILOT:
            # R3 批次18-1：PILOT abort（not_supported）→ kill + advance
            if result.status == StageStatus.ABORT_CANDIDATE:
                self._record_kill("PILOT", context, result)
                return self._advance_or_retire(campaign, context,
                                               result.data.get("reason", ""))
            return self._after_pilot(result, campaign, context)
        if stage == StageName.ANALYZE:
            # R3 批次18-1：ANALYZE abort（viewpoint_unsupported）→ kill + advance
            if result.status == StageStatus.ABORT_CANDIDATE:
                self._record_kill("ANALYZE", context, result)
                return self._advance_or_retire(campaign, context,
                                               result.data.get("reason", ""))
            return self._after_analyze(result, campaign, context)
        if stage == StageName.HUMAN_REVIEW:
            # 正常流程 HumanReviewStage 返回 paused=True → _drive 在此前就暂停了。
            # 走到这里 = 测试/mock 模式（生产入口在 _resume 的 R2 分流）。
            if self.config.harness.feature("analyze"):
                return StageName.ANALYZE
            return StageName.WRITE

        # Unknown stage or RETRY-without-progress → stop safely.
        logger.warning("no transition for stage %s (status %s) — retiring", stage, result.status)
        self._retire(campaign, context, reason=f"no_transition:{stage}")
        return None

    def _validate_paper(self, context, campaign, stage_name) -> None:
        """R6 批次18-1：WRITE/REFINE 后六节校验+lint（事件+不阻塞）。"""
        if not context.paper:
            return
        from haa.p3revise import (lint_readability, normalize_paper_v2,
                                   validate_paper_sections)
        context.paper = normalize_paper_v2(context.paper)
        sec_errors = validate_paper_sections(context.paper)
        lint_issues = lint_readability(str(context.paper))
        context.extra["paper_section_errors"] = sec_errors
        context.extra["paper_lint_issues"] = lint_issues[:10]
        if sec_errors:
            logger.warning("paper sections incomplete: %s", sec_errors[:3])
        if lint_issues:
            logger.info("readability lint: %d issue(s)", len(lint_issues))
        try:
            self.store.save_event(
                event_type="paper_section_check",
                campaign_id=campaign.id, stage=stage_name,
                payload={"errors": sec_errors,
                         "lint_count": len(lint_issues),
                         "lint_head": [i.get("type", "") for i in lint_issues[:5]]})
        except Exception:
            pass

    # -- per-stage transitions --------------------------------------------
    def _after_seek(self, result, campaign, context) -> StageName | None:
        candidates = result.data.get("candidates") or []
        context.candidates = list(candidates)
        # Preserve SEEK's structured output (open_questions, …) on the context,
        # consistent with the other stages. The large `trace` transcript is
        # handled by the observability layer (events), not the checkpoint blob.
        context.extra["seek"] = {
            k: v for k, v in result.data.items() if k not in ("candidates", "trace")
        }
        # output_candidate_count cap（P1 批量）：按 significance×win_odds（Lesson 6
        # EV 代理）排序，仅保留 top-K 进入 NOVELTY/SCREEN 筛查。被淘汰的候选标记
        # FILTERED（不静默丢弃），保留在队列中供 MORIBUND 诊断追溯。
        cap = self.config.pipeline.output_candidate_count
        if cap > 0 and len(context.candidates) > cap:
            ranked = sorted(
                context.candidates,
                key=lambda c: c.significance * c.win_odds,
                reverse=True,
            )
            keep_ids = {id(c) for c in ranked[:cap]}
            filtered = 0
            for c in context.candidates:
                if id(c) not in keep_ids:
                    c.status = CandidateStatus.FILTERED
                    filtered += 1
            logger.info(
                "SEEK output_candidate_count cap %d: %d→%d kept, %d filtered",
                cap, len(context.candidates), cap, filtered,
            )
        context.candidate = None
        context.current_candidate_id = None
        self._ensure_active_candidate(campaign, context)
        if context.candidate is None:
            self._retire(campaign, context, reason="empty_candidate_queue")
            return None
        return StageName.NOVELTY

    def _after_verify(self, result, campaign, context) -> StageName:
        """Lesson 1 + Lesson 2: carry findings forward; cap the rework loop."""
        # Lesson 1: VERIFY findings MUST survive into DESIGN on rework.
        context.verify_findings = list(result.findings)
        context.verify_passed = bool(result.data.get("verify_passed", False))
        # Preserve VERIFY's notes/counterexamples on the context (trace excluded —
        # handled by the observability layer to avoid checkpoint bloat).
        context.extra["verify"] = {
            k: v for k, v in result.data.items() if k not in ("verify_passed", "trace")
        }

        if context.verify_passed:
            return StageName.GRADE

        # A counterexample was found → rework DESIGN, if rounds remain.
        context.design_round += 1
        if context.candidate is not None:
            context.candidate.design_rounds = context.design_round

        max_rounds = self.config.pipeline.max_design_rounds
        if context.design_round < max_rounds:
            return StageName.DESIGN

        # Lesson 2: don't block — proceed to GRADE with an imperfect verify.
        logger.warning(
            "DESIGN⇄VERIFY cap (%d) reached with an open counterexample; "
            "proceeding to GRADE",
            max_rounds,
        )
        return StageName.GRADE

    def _after_grade(self, result, campaign, context) -> StageName | None:
        """Lesson 4: TRIVIAL/LOOPHOLE archive the candidate and advance."""
        context.grade = result.data
        if result.status == StageStatus.ABORT_CANDIDATE:
            self._record_kill("GRADE", context, result)
            return self._advance_or_retire(campaign, context, result.data.get("reason", ""))
        # SOLID/THIN → 设计实验规格（Phase A），不再直接进 WRITE
        return StageName.EXP_SPEC

    def _after_exp_feasibility(self, result, campaign, context) -> StageName | None:
        """实验可行性检验的转移逻辑——双层循环结构（Phase A）。

        PASS→WRITE · major(非fatal)内循环未满→回 EXP_SPEC · fatal+方案层可调+
        大循环未满→回 DESIGN(把不可行原因注入 verify_findings) · fatal 全是
        data/compute+大循环耗尽→theory-only 降级 WRITE · 其余→杀候选换队列。
        """
        blockers = result.data.get("blockers", []) or []
        fatal_blockers = [b for b in blockers if b.get("severity") == "fatal"]
        major_blockers = [b for b in blockers if b.get("severity") == "major"]
        context.extra["exp_findings"] = blockers  # 供返工使用（同 Lesson 1）

        # PASS：无 fatal/major blocker → 先导实验（特性开关 pilot 开时）
        # 或直接人工审核关卡（approve 后才 WRITE）
        if not fatal_blockers and not major_blockers:
            if self.config.harness.feature("pilot"):
                return StageName.PILOT
            return StageName.HUMAN_REVIEW

        max_exp = self.config.pipeline.max_exp_rounds
        max_exp_to_design = self.config.pipeline.max_exp_to_design_rounds

        # REWORK：major（非 fatal），内循环未满 → 回 EXP_SPEC
        if major_blockers and not fatal_blockers and context.exp_round < max_exp:
            context.exp_round += 1
            logger.info(
                "EXP_SPEC⇄EXP_FEASIBILITY rework round %d (%d major blockers)",
                context.exp_round, len(major_blockers),
            )
            return StageName.EXP_SPEC

        # major（非 fatal）内循环耗尽 → 容忍，降级进 WRITE（类比 VERIFY 耗尽→GRADE）
        if major_blockers and not fatal_blockers:
            logger.warning(
                "EXP_SPEC⇄EXP_FEASIBILITY cap (%d) reached with %d major blocker(s); "
                "proceeding to WRITE (degraded)",
                max_exp, len(major_blockers),
            )
            context.extra["exp_degraded"] = True
            return StageName.HUMAN_REVIEW

        # fatal + 大循环未满 + 方案层可调 → 回 DESIGN（不可行原因注入 verify_findings）
        if fatal_blockers and context.exp_to_design_round < max_exp_to_design:
            if self._is_design_level_blocker(fatal_blockers):
                context.exp_to_design_round += 1
                context.exp_round = 0
                context.verify_findings.extend([
                    {"kind": "exp_infeasible", "detail": b["detail"]}
                    for b in fatal_blockers
                ])
                context.design_round = 0
                logger.warning(
                    "EXP_FEASIBILITY → DESIGN (exp_to_design_round %d): "
                    "theory sound but experimentally infeasible",
                    context.exp_to_design_round,
                )
                return StageName.DESIGN

        # 大循环耗尽 + fatal 全是 data/compute → theory-only 降级 WRITE
        if fatal_blockers and all(
            b.get("category") in ("data", "compute") for b in fatal_blockers
        ):
            logger.warning(
                "EXP_FEASIBILITY: fatal data/compute blockers, "
                "proceeding as theory-only paper"
            )
            context.extra["theory_only"] = True
            return StageName.HUMAN_REVIEW

        # fatal + 判决自己给出了修复方案 + 内循环还有轮次 → 回 EXP_SPEC 返工
        #（而非直接杀）。冒烟审计 P0：候选1 的 fatal blocker 附带具体
        # fix_suggestion（"用 TD3 double critics 做反事实估计"），却因
        # _is_design_level_blocker 关键词不命中被判不可修——修复方案是
        # spec 级的，返工 EXP_SPEC 即可吸收；max_exp 轮耗尽才真正杀。
        fixable_fatal = [
            b for b in fatal_blockers if str(b.get("fix_suggestion") or "").strip()
        ]
        if fixable_fatal and context.exp_round < max_exp:
            context.exp_round += 1
            logger.info(
                "EXP_SPEC⇄EXP_FEASIBILITY rework round %d "
                "(%d fixable fatal blocker(s))",
                context.exp_round, len(fixable_fatal),
            )
            return StageName.EXP_SPEC

        # 彻底不可实验 → 杀候选，换队列
        logger.warning(
            "EXP_FEASIBILITY: candidate experimentally infeasible, aborting"
        )
        self._record_kill("EXP_FEASIBILITY", context, result)
        return self._advance_or_retire(campaign, context, "exp_infeasible")

    @staticmethod
    def _is_design_level_blocker(fatal_blockers: list[dict]) -> bool:
        """判断 fatal blocker 是否可通过调整理论方案解决（→ 回 DESIGN）。

        保守策略：仅当 category ∈ {baseline,metric,protocol} 且 fix_suggestion
        明确指向方案调整时返回 True；data/compute 类一律 False（客观限制）。
        """
        design_level_keywords = (
            "alternative approach", "different method", "reformulate",
            "调整方案", "改用", "换一个方法",
        )
        for b in fatal_blockers:
            category = b.get("category", "")
            fix = (b.get("fix_suggestion") or "").lower()
            if category in ("baseline", "metric", "protocol"):
                if any(kw in fix for kw in design_level_keywords):
                    return True
        return False

    def _after_pilot(self, result, campaign, context) -> StageName:
        """PILOT 判决转移（第三章 §8.2）：supported/partially/inconclusive
        → 人工关卡（inconclusive 携标记供人裁）；not_supported 已在阶段内
        abort_candidate（_transition 的 ABORT_CANDIDATE 路径记账死因——
        top-k 分支回退在 P1-c 接入后于此改道）。"""
        if result.data.get("verdict") == "inconclusive":
            context.extra["pilot_inconclusive"] = True
        return StageName.HUMAN_REVIEW

    def _after_analyze(self, result, campaign, context) -> StageName:
        """ANALYZE 转移（§3.1）：结果写入 extra 供 WRITE 消费。"""
        context.extra["analysis"] = result.data.get("analysis", {})
        context.extra["analysis_verdict"] = result.data.get("viewpoint_verdict", "")
        # R5 批次18-1：analyze_done 事件（不带 cost_usd）
        try:
            self.store.save_event(
                event_type="analyze_done", campaign_id=campaign.id,
                stage="ANALYZE",
                payload={"verdict": result.data.get("viewpoint_verdict", ""),
                         "supported": len(result.data.get("claims_supported", [])),
                         "unsupported": len(result.data.get("claims_unsupported", []))})
        except Exception:
            pass
        return StageName.WRITE

    def _after_review(self, result, campaign, context) -> StageName | None:
        """Lesson 3: snapshot the best; cap the refine loop."""
        context.review = result.data
        self._maybe_save_best_snapshot(context)

        decision = str(result.data.get("decision", "reject")).strip().lower()
        if decision == "accept":
            self._publish(campaign, context, degraded=False)
            return None

        context.review_round += 1
        if context.candidate is not None:
            context.candidate.review_rounds = context.review_round

        max_rounds = self.config.pipeline.max_review_rounds
        if context.review_round < max_rounds:
            return StageName.REFINE

        # Cap reached still rejecting → publish, but mark degraded.
        logger.warning(
            "REVIEW⇄REFINE cap (%d) reached still rejecting; publishing degraded",
            max_rounds,
        )
        self._publish(campaign, context, degraded=True)
        return None

    # -- candidate queue (Lesson 4) ---------------------------------------
    def _record_kill(self, stage_name: str, context, result) -> None:
        """Persist a candidate-kill verdict into ``context.extra["kills"]``.

        Must run BEFORE ``_advance_or_retire`` resets the per-candidate context —
        otherwise the kill evidence (kill_method / evidence / rationale) is lost
        and the ARV-stage audit cannot review whether the kill was justified
        (错杀 vs 漏杀). ``extra`` survives the per-candidate reset.

        大修批次2（第二章 §8 墓穴即时版）：kill 记录在旧字段（stage/slug/
        title/verdict/reason）之上扩展 core_claim / kill_evidence /
        structure_fingerprint——campaign 内 SEEK 第二轮起的死路清单注入与
        campaign 结束的持久库转写都以本记录为数据源。
        """
        kills = context.extra.setdefault("kills", [])
        cand = context.candidate
        verdict = {
            k: v for k, v in result.data.items() if k not in ("trace", "candidates")
        }
        record = {
            "stage": stage_name,
            "slug": cand.slug if cand is not None else None,
            "title": cand.title if cand is not None else None,
            "verdict": verdict,
            "reason": str(result.data.get("reason", "")),
        }
        # --- 批次2 扩展字段（旧读方按 key 取，缺省无害） ---
        core_claim = ""
        if cand is not None:
            core_claim = str(getattr(cand, "positive_claim", "") or "").strip()
            if not core_claim:
                core_claim = str(getattr(cand, "rationale", "") or "").strip()[:500]
        if core_claim:
            record["core_claim"] = core_claim
        evidence = self._extract_kill_evidence(result.data)
        if evidence:
            record["kill_evidence"] = evidence
        fingerprint = result.data.get("structure_fingerprint")
        if isinstance(fingerprint, dict) and fingerprint:
            # 第三章 §6 的结构指纹（NOVELTY 入口生成后自然填充；当前管线
            # 尚无生产者，字段先行占位）
            record["structure_fingerprint"] = fingerprint
        kills.append(record)
        # P1-c：分支树同步标记（feature 开时）
        tree = (context.extra or {}).get("branch_tree")
        if tree is not None and cand is not None:
            tree.kill_branch(cand.slug, stage=stage_name,
                             reason=record.get("reason", ""))

    @staticmethod
    def _extract_kill_evidence(result_data: dict) -> str:
        """Best-effort 抽取本次杀候选的硬证据（SCREEN 反例/NOVELTY 撞车/
        EXP blockers/VERIFY findings）。确定性抽取，不做模型调用。"""
        for key in ("kill_evidence", "evidence", "counterexample"):
            v = result_data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:2000]
            if isinstance(v, list) and v:
                return "; ".join(str(x) for x in v)[:2000]
        blockers = result_data.get("blockers")
        if isinstance(blockers, list):
            parts = [str(b.get("evidence")) for b in blockers
                     if isinstance(b, dict) and b.get("evidence")]
            if parts:
                return "; ".join(parts)[:2000]
        findings = result_data.get("findings")
        if isinstance(findings, list):
            parts = [str(f.get("evidence") or f.get("detail") or "")
                     for f in findings if isinstance(f, dict)]
            parts = [p for p in parts if p]
            if parts:
                return "; ".join(parts)[:2000]
        return ""

    def _advance_or_retire(self, campaign, context, reason) -> StageName | None:
        """Archive the current candidate and advance to the next, or retire."""
        from haa.artifacts import archive_rejected_candidate

        killed_slug = None
        if context.candidate is not None:
            context.candidate.status = CandidateStatus.DEAD
            killed_slug = context.candidate.slug
        context.candidate = None
        context.current_candidate_id = None
        campaign.current_candidate_id = None
        self._reset_candidate_context(context)
        # 归档被杀候选的工件到 _rejected/<slug>/（镜像上下文重置的隔离语义，
        # 历史仍可查——ARV 复核"错杀"的磁盘侧依据）。
        if killed_slug:
            archive_rejected_candidate(
                self.config.storage.resolved_campaigns_dir(self.config.project_root),
                campaign.id, killed_slug,
            )
        self._ensure_active_candidate(campaign, context)
        if context.candidate is None:
            self._retire(campaign, context, reason="queue_exhausted")
            return None
        logger.info("candidate advanced (%s); restarting at NOVELTY", reason)
        return StageName.NOVELTY

    def _ensure_active_candidate(self, campaign, context) -> None:
        """Select the first live candidate in the queue as the active one."""
        if context.candidate is not None and not context.candidate.is_terminal:
            return
        remaining = [c for c in context.candidates if not c.is_terminal]
        if not remaining:
            context.candidate = None
            return
        cand = remaining[0]
        if cand.status == CandidateStatus.PROPOSED:
            cand.status = CandidateStatus.ACTIVE
        context.candidate = cand
        context.current_candidate_id = cand.id
        campaign.current_candidate_id = cand.id

    @staticmethod
    def _reset_candidate_context(context) -> None:
        """Clear per-candidate working memory for a fresh candidate."""
        context.novelty = {}
        context.screen = {}
        context.design = {}
        context.verify_findings = []
        context.verify_passed = False
        context.grade = {}
        context.paper = {}
        context.review = {}
        context.best_snapshot = None
        context.design_round = 0
        context.review_round = 0
        context.exp_round = 0
        context.exp_to_design_round = 0

    # -- Lesson 3 best-snapshot -------------------------------------------
    def _maybe_save_best_snapshot(self, context) -> None:
        """Save the paper+review if this round beat (or tied) the best so far."""
        current = (context.review or {}).get("overall")
        if current is None:
            return
        best = context.best_snapshot
        best_overall = (best.get("review") or {}).get("overall") if best else None
        if best_overall is None or current >= best_overall:
            snap = context.snapshot_paper()
            snap["review_round"] = context.review_round
            context.best_snapshot = snap

    # -- terminal helpers --------------------------------------------------
    def _publish(self, campaign, context, *, degraded: bool = False) -> None:
        if degraded:
            self._maybe_publish_best_instead(context)
        if context.candidate is not None:
            context.candidate.status = CandidateStatus.PUBLISHED
            if context.candidate.grade is None:
                context.candidate.grade = GradeVerdict.SOLID
        if degraded:
            context.paper["degraded"] = True
        campaign.status = CampaignStatus.PUBLISHED
        self._persist(campaign, context, "PUBLISHED")

    @staticmethod
    def _maybe_publish_best_instead(context) -> None:
        """Cap-degraded publish: swap in the best snapshot if it scored higher.

        smoke4 实证：REVIEW 分数 0.593 → … → 0.517 逐轮下滑，best_snapshot
        只用于 REFINE 前回滚、不用于发布——最后发的是最差的那版。HM-Pro
        教训 3 的自然延伸："发布已知最好的那版"。非降级路径不受影响（能过审
        的最后一版按定义不差于历史最佳——过审即 accept）。
        """
        best = context.best_snapshot
        if not best:
            return
        best_paper = best.get("paper") or {}
        best_overall = (best.get("review") or {}).get("overall")
        cur_overall = (context.review or {}).get("overall")
        if best_paper and best_overall is not None and (
            cur_overall is None or best_overall > cur_overall
        ):
            logger.info(
                "degraded publish: swapping in best snapshot (overall %.3f > final %.3f)",
                best_overall, cur_overall if cur_overall is not None else float("nan"),
            )
            context.paper = best_paper
            context.review = best.get("review") or context.review

    def _retire(self, campaign, context, *, reason: str = "") -> None:
        if context.candidate is not None and not context.candidate.is_terminal:
            context.candidate.status = CandidateStatus.DEAD
        campaign.status = CampaignStatus.RETIRED
        self._persist(campaign, context, f"RETIRED:{reason}")

    # -- persistence -------------------------------------------------------
    def _persist(self, campaign, context, label: str) -> None:
        """Atomically write campaign + dirty candidates + a checkpoint."""
        # CRITICAL: re-sync budget fields from the store before saving.
        # BudgetManager.pre_spend/record update budget_used on a *fresh* campaign
        # object (its own get_campaign); this pipeline holds a separate in-memory
        # campaign whose budget_used is stale. Without this sync,
        # commit_transition→save_campaign would overwrite the stored budget_used
        # with the stale value — which also silently disabled the budget gate
        # (pre_spend reads the stored value, so a clobbered-to-0 budget_used
        # means "always full budget, never gate"). See HM-Pro smoke finding.
        fresh = self.store.get_campaign(campaign.id)
        if fresh is not None:
            campaign.budget_used = fresh.budget_used
            campaign.budget_limit = fresh.budget_limit
        if context.candidates:
            candidates = list(context.candidates)
        elif context.candidate is not None:
            candidates = [context.candidate]
        else:
            candidates = None
        self.store.commit_transition(
            campaign,
            stage=label,
            context=context.to_checkpoint_dict(),
            candidates=candidates,
        )
        # 中间产出工件化（v0.9.1）：结构化产出幂等落盘 + MANIFEST。
        # 只在 :done / 终态时写（:start 上下文未变）；写失败不影响主管线。
        if not label.endswith(":start"):
            from haa.artifacts import write_artifacts

            write_artifacts(
                self.config.storage.resolved_campaigns_dir(self.config.project_root),
                campaign.id, context, label,
            )
