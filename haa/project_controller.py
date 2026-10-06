"""ProjectController — the macro state machine (PR→P1→ARV→P2→EA→P3).

This is HAA's outer framework. A :class:`Project` owns multiple Campaigns
across three phases; the controller orchestrates them, enforces the five
Project lifecycle states (NOT_STARTED / IN_PROGRESS / MORIBUND / COMPLETED /
ABORTED), and drives the P1 batch precursor-production loop (scout + worker
Campaigns).

v0.4 scope: Project model + state machine + P1 batch. P2/P3 are stubs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from haa.config import Config
from haa.models import (
    Brief,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    GradeVerdict,
    MoribundEntry,
    Phase,
    Precursor,
    Project,
    ProjectHyperparams,
    ProjectStatus,
)
from haa.pipeline import Pipeline
from haa.prompts import render_prompt
from haa.state import StateStore

logger = logging.getLogger("haa.project")


class ProjectController:
    """Drive a Project through the macro state machine.

    Parameters
    ----------
    config:
        Loaded :class:`~haa.config.Config`.
    store:
        :class:`~haa.state.StateStore` for persistence.
    budget:
        :class:`~haa.budget.BudgetManager`.
    llm:
        :class:`~haa.llm.LLMClient` for the MORIBUND diagnostic call.
    pipeline:
        Optional injected :class:`~haa.pipeline.Pipeline` — used directly for
        ALL run_campaign calls (testing). If None, a fresh Pipeline is built
        per scout run with the project's hyperparams applied.
    """

    def __init__(
        self,
        config: Config,
        store: StateStore,
        budget: Any,
        *,
        llm: Any | None = None,
        pipeline: Pipeline | Any | None = None,
        coding_agent_factory: Any | None = None,
        p2_transport: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.budget = budget
        self.llm = llm
        self._pipeline = pipeline  # injected (testing) or None (production)
        self._coding_agent_factory = coding_agent_factory
        self._p2_transport = p2_transport

    # ------------------------------------------------------------------ #
    #  Queries
    # ------------------------------------------------------------------ #

    def get_project(self, project_id: str) -> Project | None:
        return self.store.get_project(project_id)

    def list_projects(self) -> list[Project]:
        return self.store.list_projects()

    def _require(self, project_id: str) -> Project:
        p = self.store.get_project(project_id)
        if p is None:
            raise KeyError(f"unknown project {project_id}")
        return p

    # ------------------------------------------------------------------ #
    #  Creation (PR phase)
    # ------------------------------------------------------------------ #

    def create_project(
        self,
        brief: Brief,
        hyperparams: ProjectHyperparams | None = None,
    ) -> Project:
        """Create a Project in NOT_STARTED / PR phase."""
        project = Project(
            brief=brief,
            hyperparams=hyperparams or ProjectHyperparams(),
        )
        self.store.save_project(project)
        logger.info("project %s created (brief='%s')", project.id, brief.title)
        return project

    # ------------------------------------------------------------------ #
    #  Human switch → P1 batch
    # ------------------------------------------------------------------ #

    def start_project(self, project_id: str) -> Project:
        """Flip the human switch: NOT_STARTED → IN_PROGRESS, run P1 batch."""
        project = self._require(project_id)
        if project.status != ProjectStatus.NOT_STARTED:
            raise ValueError(
                f"project {project_id} not in NOT_STARTED (is {project.status.value})"
            )
        project.status = ProjectStatus.IN_PROGRESS
        project.phase = Phase.P1
        self.store.save_project(project)
        return self.run_p1_batch(project)

    # ------------------------------------------------------------------ #
    #  P1 batch (the core v0.4 orchestration)
    # ------------------------------------------------------------------ #

    def resume_p1_batch(self, project_id: str) -> Project:
        """Resume an interrupted P1 batch（v1.0.2 断点恢复，smoke4 事故产物）。

        run_p1_batch 只能从零开始（scout 新建）；进程被杀/挂死后整批报废。
        本方法续跑：

        1. scout 未终态 → 从 checkpoint 恢复续跑（可能补发前体）
        2. 已有 worker 链接 campaign 非终态 → 从 checkpoint 恢复续跑
        3. scout 队列中 PROPOSED 且尚无 worker 的候选 → 补建 worker
           （按 slug 对账，不重复建）
        4. 收尾与 run_p1_batch 相同：harvest、记忆转写、ARV/MORIBUND
        """
        project = self._require(project_id)
        if project.phase != Phase.P1:
            raise ValueError(
                f"project {project_id} not in P1 (is {project.phase.value})"
            )
        pipeline = self._resolve_pipeline(project)
        links = self.store.list_campaigns_for_project(project.id)
        scout_id = next((cid for cid, role in links if role == "scout"), None)
        if scout_id is None:
            raise ValueError(
                f"project {project_id} has no scout campaign; use start_project"
            )

        def _harvest_once(cid: str) -> None:
            pre = self.harvest_precursor(cid)
            if pre is not None and not any(
                p.campaign_id == pre.campaign_id for p in project.precursors
            ):
                project.precursors.append(pre)

        # --- 1. scout：非终态先恢复；无论终态与否都补 harvest ---
        #（crash 可能发生在 harvest 之后、save_project 之前——前体没落库；
        #  _harvest_once 的 campaign_id 去重保证不重复。）
        scout = self.store.get_campaign(scout_id)
        if scout is not None and not scout.is_terminal:
            logger.info("project %s: resuming scout campaign %s", project.id, scout_id)
            self._run_campaign_through_gate(
                pipeline, scout_id, project.brief, project
            )
        _harvest_once(scout_id)

        # --- 2. 恢复非终态 worker；对账已处理 slug；终态 worker 补 harvest ---
        handled_slugs: set[str] = set()
        for cid, role in links:
            if role != "worker":
                continue
            c = self.store.get_campaign(cid)
            if c is None:
                continue
            for cand in self.store.list_candidates(cid):
                handled_slugs.add(cand.slug)
            if not c.is_terminal:
                logger.info(
                    "project %s: resuming worker campaign %s from %s",
                    project.id, cid, c.status.value,
                )
                self._run_campaign_through_gate(
                    pipeline, cid, self._make_worker_brief(project.brief), project
                )
            _harvest_once(cid)

        # --- 3. 补建剩余候选的 worker（镜像 run_p1_batch） ---
        scout_cands = self.store.list_candidates(scout_id)
        published = {
            c.id for c in scout_cands if c.status == CandidateStatus.PUBLISHED
        }
        for scout_cand in scout_cands:
            if scout_cand.status != CandidateStatus.PROPOSED:
                continue
            if scout_cand.id in published or scout_cand.slug in handled_slugs:
                continue
            worker_brief = self._make_worker_brief(project.brief)
            worker_campaign = self.store.create_campaign(
                worker_brief, budget_limit=self.config.budget.per_campaign
            )
            worker_cand = self._clone_candidate(scout_cand, worker_campaign.id)
            self.store.save_candidate(worker_cand)
            self.store.link_campaign(project.id, worker_campaign.id, role="worker")
            logger.info(
                "project %s: worker campaign %s for candidate '%s' (resume)",
                project.id, worker_campaign.id, scout_cand.slug,
            )
            self._run_campaign_through_gate(
                pipeline, worker_campaign.id, worker_brief, project
            )
            _harvest_once(worker_campaign.id)

        # --- 4. 收尾（同 run_p1_batch 尾部） ---
        self._record_to_memory(project, scout_id)
        self.store.save_project(project)
        if not project.precursors:
            logger.warning(
                "project %s: resumed P1 produced 0 precursors → MORIBUND", project.id
            )
            return self.set_moribund(project, reason="p1_no_precursors")
        project.phase = Phase.ARV
        self.store.save_project(project)
        logger.info(
            "project %s: P1 batch resumed complete, %d precursor(s), phase→ARV",
            project.id, len(project.precursors),
        )
        return project

    def run_p1_batch(self, project: Project) -> Project:
        """Scout + worker Campaign orchestration → multiple precursors.

        （断点恢复场景请用 :meth:`resume_p1_batch`——本方法总是新建 scout。）

        Flow:
        1. Run scout Campaign (full pipeline SEEK→…→PUBLISHED).
        2. Harvest scout precursor (if PUBLISHED).
        3. Collect remaining PROPOSED candidates from scout's queue.
        4. For each, spawn a worker Campaign (skip_to="novelty" + pre-seeded).
        5. Harvest each worker precursor.
        6. 0 precursors → MORIBUND + LLM diagnostic; ≥1 → phase=ARV.
        """
        pipeline = self._resolve_pipeline(project)

        # --- 1. Scout Campaign (full pipeline) ---
        scout_campaign = self.store.create_campaign(
            project.brief, budget_limit=self.config.budget.per_campaign
        )
        self.store.link_campaign(project.id, scout_campaign.id, role="scout")
        logger.info("project %s: scout campaign %s started", project.id, scout_campaign.id)

        self._run_campaign_through_gate(
            pipeline, scout_campaign.id, project.brief, project
        )
        scout_pre = self.harvest_precursor(scout_campaign.id)
        if scout_pre is not None:
            project.precursors.append(scout_pre)

        # --- 2. Collect remaining PROPOSED candidates ---
        scout_cands = self.store.list_candidates(scout_campaign.id)
        published_ids = {
            c.id for c in scout_cands if c.status == CandidateStatus.PUBLISHED
        }
        remaining = [
            c for c in scout_cands
            if c.status == CandidateStatus.PROPOSED and c.id not in published_ids
        ]
        logger.info(
            "project %s: scout done (%d precursor(s), %d remaining candidate(s))",
            project.id, len(project.precursors), len(remaining),
        )

        # --- 3. Worker Campaigns (one per remaining candidate) ---
        for scout_cand in remaining:
            worker_brief = self._make_worker_brief(project.brief)
            worker_campaign = self.store.create_campaign(
                worker_brief, budget_limit=self.config.budget.per_campaign
            )
            worker_cand = self._clone_candidate(scout_cand, worker_campaign.id)
            self.store.save_candidate(worker_cand)
            self.store.link_campaign(project.id, worker_campaign.id, role="worker")
            logger.info(
                "project %s: worker campaign %s for candidate '%s'",
                project.id, worker_campaign.id, scout_cand.slug,
            )

            self._run_campaign_through_gate(
                pipeline, worker_campaign.id, worker_brief, project
            )
            wp = self.harvest_precursor(worker_campaign.id)
            if wp is not None:
                project.precursors.append(wp)

        # --- 4. Decide next phase ---
        self._record_to_memory(project, scout_campaign.id)
        self.store.save_project(project)
        if not project.precursors:
            logger.warning("project %s: P1 produced 0 precursors → MORIBUND", project.id)
            return self.set_moribund(project, reason="p1_no_precursors")

        project.phase = Phase.ARV
        self.store.save_project(project)
        logger.info(
            "project %s: P1 batch complete, %d precursor(s), phase→ARV",
            project.id, len(project.precursors),
        )
        return project

    def _record_to_memory(self, project: Project, scout_campaign_id: str) -> None:
        """P1 结束时把全部候选（含被杀的）转写为跨项目记忆页（墓志铭机制）。

        数据源：artifacts 目录（v0.9.1 落盘的 candidates.json + kills.json），
        回退到 store 的候选与 checkpoint kills。写失败吞掉——记忆层绝不
        影响主管线。
        """
        if not self.config.memory.enabled:
            return
        try:
            from haa.memory import MemoryStore, idea_pages_from_campaign

            campaigns_dir = self.config.storage.resolved_campaigns_dir(
                self.config.project_root
            )
            candidates: list | None = None
            kills: list | None = None
            # 首选 artifacts（含全量候选与 kill 证据）。
            cand_file = campaigns_dir / scout_campaign_id / "artifacts" / "candidates.json"
            kills_file = campaigns_dir / scout_campaign_id / "artifacts" / "kills.json"
            if cand_file.exists():
                candidates = json.loads(cand_file.read_text(encoding="utf-8"))
            if kills_file.exists():
                kills = json.loads(kills_file.read_text(encoding="utf-8"))
            # 回退：store 候选 + 最新 checkpoint 的 extra.kills。
            if candidates is None:
                candidates = [
                    c.model_dump(mode="json")
                    for c in self.store.list_candidates(scout_campaign_id)
                ]
            if kills is None:
                cp = self.store.latest_checkpoint(scout_campaign_id)
                kills = ((cp.context or {}).get("extra") or {}).get("kills") or []
            if not candidates:
                return
            pages = idea_pages_from_campaign(
                scout_campaign_id, candidates, kills or [],
                origin_project=project.id,
                origin_brief_title=project.brief.title,
            )
            mem = MemoryStore(
                self.config.project_root / self.config.memory.memory_dir,
                max_inject=self.config.memory.injection_max_items,
            )
            mem.record_batch(pages)
            logger.info(
                "project %s: %d candidate(s) recorded to memory (%s)",
                project.id, len(pages), mem.root,
            )
        except Exception as exc:  # noqa: BLE001 — 记忆层绝不影响主管线
            logger.warning("_record_to_memory failed: %s", exc)

    def _run_campaign_through_gate(
        self, pipeline, campaign_id: str, brief: Brief | None, project: Project
    ):
        """Run a campaign, auto-approving HUMAN_REVIEW if configured.

        When auto_approve_human_review is True (default), a Campaign that
        pauses at AWAITING_HUMAN_REVIEW is immediately resumed (equivalent to
        ``haa approve``) so the batch runs autonomously. The real paper-level
        review happens at ARV.
        """
        result = pipeline.run_campaign(campaign_id, brief=brief)
        if (
            result.status == CampaignStatus.AWAITING_HUMAN_REVIEW
            and project.hyperparams.auto_approve_human_review
        ):
            logger.info(
                "auto-approving HUMAN_REVIEW for campaign %s", campaign_id
            )
            result = pipeline.run_campaign(campaign_id)  # resumes at WRITE per _resume
        return result

    # ------------------------------------------------------------------ #
    #  P2: experiment execution (v0.7)
    # ------------------------------------------------------------------ #

    def advance_to_p2(self, project_id: str) -> Project:
        """ARV→P2: user has selected a precursor; start experiment execution.

        Requires ``selected_precursor_campaign_id`` to be set (via
        :meth:`select_precursor`).
        """
        project = self._require(project_id)
        if project.phase != Phase.ARV:
            raise ValueError(
                f"advance_to_p2 only valid in ARV phase (is {project.phase.value})"
            )
        if not project.selected_precursor_campaign_id:
            raise ValueError("no precursor selected — call select_precursor first")
        project.phase = Phase.P2
        self.store.save_project(project)
        return self._run_p2_batch(project)

    def _run_p2_batch(self, project: Project) -> Project:
        """P2 orchestration: CODE_GEN → EXECUTE(DebugSession) → ANALYZE.

        On circuit-breaker failure → MORIBUND + P2 diagnostic.
        On success → phase=EA (experiment analysis, human gate).
        """
        precursor = self._get_selected_precursor(project)
        if precursor is None:
            raise ValueError("selected precursor not found among project precursors")

        # Work directory for P2 artifacts.
        work_dir = self.config.storage.resolved_campaigns_dir(self.config.project_root)
        p2_dir = work_dir / project.id / "p2"
        p2_dir.mkdir(parents=True, exist_ok=True)

        # 1. CODE_GEN
        coding_agent = self._build_coding_agent(project, p2_dir)
        code_dir = self._p2_code_gen(project, precursor, coding_agent, p2_dir)
        project.exp_code_dir = str(code_dir)
        self.store.save_project(project)

        # 2. EXECUTE (DebugSession)
        debug_result = self._p2_execute(project, code_dir, coding_agent, p2_dir)
        if not debug_result.success:
            logger.warning("project %s: P2 EXECUTE failed → MORIBUND", project.id)
            return self._set_moribund_p2(project, debug_result, precursor)

        # 3. ANALYZE
        analysis = self._p2_analyze(project, precursor, debug_result)
        project.exp_results = {
            "metrics": debug_result.metrics,
            "analysis": analysis,
            "rounds_a": debug_result.rounds_a,
            "rounds_b": debug_result.rounds_b,
            "results_dir": str(debug_result.results_dir) if debug_result.results_dir else "",
        }

        # 4. Advance to EA
        project.phase = Phase.EA
        self.store.save_project(project)
        logger.info("project %s: P2 complete, phase→EA", project.id)
        return project

    def _p2_code_gen(self, project, precursor, coding_agent, p2_dir) -> Path:
        """CODE_GEN: Claude Code generates experiment code."""
        logger.info("project %s: P2 CODE_GEN starting", project.id)
        exp_spec = precursor.exp_spec or {}
        paper_dict = {
            "candidate_title": precursor.candidate_title,
            "paper": precursor.paper,
        }
        result = coding_agent.generate_code(paper_dict, exp_spec)
        if not result.success:
            raise RuntimeError(f"P2 CODE_GEN failed: {result.error}")
        code_dir = result.work_dir or p2_dir / "code"
        logger.info("project %s: P2 CODE_GEN done (code_dir=%s)", project.id, code_dir)
        return Path(code_dir)

    def _p2_execute(self, project, code_dir, coding_agent, p2_dir):
        """EXECUTE: DebugSession dual-phase circuit breaker."""
        from haa.p2.debug_session import DebugConfig, DebugSession

        hp = project.hyperparams
        debug_config = DebugConfig(
            max_hard_error_rounds=hp.max_hard_rounds if hasattr(hp, "max_hard_rounds") else 15,
            max_logic_error_rounds=hp.max_logic_rounds if hasattr(hp, "max_logic_rounds") else 5,
            auto_approve_a_to_b=True,
        )
        transport = self._build_p2_transport()
        session = DebugSession(
            transport=transport,
            coding_agent=coding_agent,
            config=debug_config,
            code_dir=code_dir,
            work_dir=p2_dir / "debug",
            campaign_id=project.id,
        )
        return session.run()

    def _p2_analyze(self, project, precursor, debug_result) -> dict:
        """ANALYZE: LLM analyzes experiment results → structured summary."""
        import json as _json

        log_tail = debug_result.log[-2000:] if debug_result.log else ""
        prompt = render_prompt(
            "p2_analyze",
            precursor=precursor,
            exp_spec=precursor.exp_spec or {},
            metrics=debug_result.metrics,
            rounds_a=debug_result.rounds_a,
            rounds_b=debug_result.rounds_b,
            log_tail=log_tail,
        )
        try:
            resp = self.llm.call(
                messages=[{"role": "user", "content": prompt}],
                stage="P2_ANALYZE",
            )
            # Try to parse JSON; fall back to raw content.
            try:
                return _json.loads(resp.content)
            except (ValueError, _json.JSONDecodeError):
                return {"raw_analysis": resp.content}
        except Exception as exc:
            logger.warning("P2 ANALYZE LLM call failed: %s", exc)
            return {"error": str(exc), "metrics": debug_result.metrics}

    def _set_moribund_p2(self, project, debug_result, precursor) -> Project:
        """P2 MORIBUND: experiment failed → diagnostic + MORIBUND state."""
        prompt = render_prompt(
            "p2_diagnostic",
            precursor=precursor,
            exp_spec=precursor.exp_spec or {},
            rounds_a=debug_result.rounds_a,
            rounds_b=debug_result.rounds_b,
            max_hard_rounds=15,
            max_logic_rounds=5,
            failed_phase=debug_result.phase,
            failure_reason=debug_result.reason,
            error_log=debug_result.log or debug_result.error,
            metrics=debug_result.metrics,
        )
        try:
            resp = self.llm.call(
                messages=[{"role": "user", "content": prompt}],
                stage="P2_DIAGNOSTIC",
            )
            diagnostic = resp.content
        except Exception as exc:
            logger.warning("P2 diagnostic LLM call failed: %s", exc)
            diagnostic = f"(LLM diagnostic unavailable) {debug_result.reason}: {debug_result.error}"

        project.moribund_reason = f"p2_{debug_result.reason}"
        project.moribund_diagnostic = diagnostic
        project.moribund_history.append(
            MoribundEntry(
                phase=project.phase.value,
                reason=f"p2_{debug_result.reason}",
                diagnostic=diagnostic,
            )
        )
        project.status = ProjectStatus.MORIBUND
        self.store.save_project(project)
        return project

    def _get_selected_precursor(self, project: Project) -> Precursor | None:
        """Find the selected precursor among the project's precursors."""
        for pre in project.precursors:
            if pre.campaign_id == project.selected_precursor_campaign_id:
                return pre
        return None

    def _build_coding_agent(self, project: Project, work_dir: Path):
        """Construct the coding agent (ClaudeCodeACP). Tests can inject a factory."""
        if self._coding_agent_factory is not None:
            return self._coding_agent_factory(project, work_dir)
        from haa.coding_agent import ClaudeCodeACP

        campaign_id = project.selected_precursor_campaign_id or project.id
        return ClaudeCodeACP(campaign_id, work_dir, self.config.acp)

    def _build_p2_transport(self):
        """Construct the P2 execution transport (SSH if configured, else Local)."""
        if self._p2_transport is not None:
            return self._p2_transport
        if self.config.ssh.host:
            from haa.p2.transport import SSHDebugTransport
            return SSHDebugTransport(self.config.ssh)
        from haa.p2.transport import LocalDebugTransport
        return LocalDebugTransport()

    # ------------------------------------------------------------------ #
    #  P3: LaTeX paper production (v0.9)
    # ------------------------------------------------------------------ #

    def advance_to_p3(self, project_id: str) -> Project:
        """EA→P3: user confirmed experiment results are satisfactory."""
        project = self._require(project_id)
        if project.phase != Phase.EA:
            raise ValueError(
                f"advance_to_p3 only valid in EA phase (is {project.phase.value})"
            )
        project.phase = Phase.P3
        self.store.save_project(project)
        return self._run_p3_batch(project)

    def _run_p3_batch(self, project: Project) -> Project:
        """P3 orchestration: PAPER_INTEGRATE → MD2LATEX → LATEX_COMPILE → PACKAGE.

        P3 原则上不 moribund（图表核验不过→放弃该图，文字替代）。
        """
        from haa.p3.md_to_latex import SECTION_ORDER, assemble_paper, convert_section
        from haa.p3.latex_compiler import LatexCompiler
        from haa.p3.packager import package_deliverable

        precursor = self._get_selected_precursor(project)
        if precursor is None:
            raise ValueError("no selected precursor for P3")

        work_dir = self.config.storage.resolved_campaigns_dir(self.config.project_root)
        p3_dir = work_dir / project.id / "p3"
        paper_dir = p3_dir / "paper"
        p3_dir.mkdir(parents=True, exist_ok=True)

        # --- 1. Prerequisite check ---
        if not project.exp_results:
            raise ValueError("P3 requires P2 experiment results (project.exp_results empty)")

        # --- 2. PAPER_INTEGRATE ---
        sections_md = self._p3_paper_integrate(project, precursor)
        if not sections_md:
            raise RuntimeError("P3 PAPER_INTEGRATE produced no sections")

        # --- 3. MD2LATEX (per-section conversion) ---
        sections_latex: dict[str, str] = {}
        for name in SECTION_ORDER:
            md = sections_md.get(name, "")
            if not md:
                continue
            latex = convert_section(name, md, self.llm)
            sections_latex[name] = latex
            logger.info("project %s: P3 MD2LATEX %s done", project.id, name)

        main_tex = assemble_paper(
            paper_dir, sections_latex,
            title=precursor.candidate_title,
            authors="Anonymous",
        )
        project.p3_paper_dir = str(paper_dir)

        # --- 4. FIGURE_VERIFY (v0.9: theory papers have no image figures → skip) ---
        # HAA 论文以表格/算法为主（参考 HM-Pro 理论论文路线），暂无 \includegraphics。
        # v1.0+ 加入实验图表时再启用 describe_image 核验。

        # --- 5. LATEX_COMPILE ---
        compiler = LatexCompiler()
        compile_result = compiler.compile(paper_dir)
        if compile_result.degraded:
            logger.warning(
                "project %s: pdflatex not installed — .tex source generated (degraded)",
                project.id,
            )

        # --- 6. PACKAGE ---
        deliverable = package_deliverable(
            paper_dir=paper_dir,
            output_path=p3_dir / f"{project.id}_deliverable.zip",
            code_dir=project.exp_code_dir or None,
            results_dir=project.exp_results.get("results_dir") or None,
            project_title=precursor.candidate_title,
        )
        project.p3_deliverable_path = str(deliverable)

        # --- 7. Done ---
        project.phase = Phase.DONE
        self.store.save_project(project)
        logger.info(
            "project %s: P3 complete, deliverable=%s, phase→DONE",
            project.id, deliverable,
        )
        return project

    def _p3_paper_integrate(self, project: Project, precursor: Precursor) -> dict:
        """PAPER_INTEGRATE: LLM 整合 P1 理论 + P2 实验 → 完整论文 markdown。"""
        import json as _json

        exp_metrics = project.exp_results.get("metrics", {})
        exp_analysis = project.exp_results.get("analysis", {})
        prompt = render_prompt(
            "p3_paper_integrate",
            precursor=precursor,
            exp_metrics=exp_metrics,
            exp_analysis=exp_analysis,
        )
        try:
            resp = self.llm.call(
                messages=[{"role": "user", "content": prompt}],
                stage="P3_PAPER_INTEGRATE",
            )
            return _json.loads(resp.content)
        except (ValueError, _json.JSONDecodeError):
            logger.warning("P3 PAPER_INTEGRATE did not return valid JSON — using raw content")
            return {"abstract": resp.content}
        except Exception as exc:
            logger.error("P3 PAPER_INTEGRATE failed: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    #  Precursor harvesting
    # ------------------------------------------------------------------ #

    def harvest_precursor(self, campaign_id: str) -> Precursor | None:
        """Extract a Precursor from a PUBLISHED campaign's latest checkpoint.

        Returns None if the campaign is not PUBLISHED or has no checkpoint.
        """
        snap = self.store.restore(campaign_id)
        if snap is None or not snap.has_checkpoint:
            return None
        if snap.campaign.status != CampaignStatus.PUBLISHED:
            return None

        ctx = snap.checkpoint.context
        cand_dict = ctx.get("candidate") or {}
        extra = ctx.get("extra") or {}

        grade_value = cand_dict.get("grade")
        if isinstance(grade_value, dict):
            grade_value = grade_value.get("grade")

        return Precursor(
            campaign_id=campaign_id,
            candidate_id=cand_dict.get("id", ""),
            candidate_slug=cand_dict.get("slug", ""),
            candidate_title=cand_dict.get("title", snap.campaign.title),
            grade=grade_value,
            paper=ctx.get("paper") or {},
            review=ctx.get("review") or {},
            exp_spec=extra.get("exp_spec") or {},
            exp_degraded=bool(extra.get("exp_degraded", False)),
            theory_only=bool(extra.get("theory_only", False)),
            created_at=snap.checkpoint.created_at,
        )

    # ------------------------------------------------------------------ #
    #  ARV (human re-verification)
    # ------------------------------------------------------------------ #

    def select_precursor(self, project_id: str, campaign_id: str) -> Project:
        """ARV→P2 boundary: user picks one precursor.

        v0.4 records the selection but does NOT advance to P2 (P2 doesn't
        exist yet). v0.7 will advance phase→P2 here.
        """
        project = self._require(project_id)
        if project.phase != Phase.ARV:
            raise ValueError(
                f"select_precursor only valid in ARV phase (is {project.phase.value})"
            )
        if not any(p.campaign_id == campaign_id for p in project.precursors):
            raise ValueError(
                f"campaign {campaign_id} not among project {project_id} precursors"
            )
        project.selected_precursor_campaign_id = campaign_id
        self.store.save_project(project)
        logger.info("project %s: precursor %s selected at ARV", project_id, campaign_id)
        return project

    def select_precursors(self, project_id: str, campaign_ids: list[str]) -> Project:
        """ARV 多选（v1.0.2）：勾选多个合格前体。

        首个同步进 ``selected_precursor_campaign_id``（下游 P2/P3 状态机按
        单前体消费，保持兼容）；完整有序清单存
        ``selected_precursor_campaign_ids``。
        """
        project = self._require(project_id)
        if project.phase != Phase.ARV:
            raise ValueError(
                f"select_precursors only valid in ARV phase (is {project.phase.value})"
            )
        known = {p.campaign_id for p in project.precursors}
        cleaned = list(dict.fromkeys(c.strip() for c in campaign_ids if c.strip()))
        unknown = [c for c in cleaned if c not in known]
        if not cleaned:
            raise ValueError("select_precursors: no campaign ids given")
        if unknown:
            raise ValueError(
                f"campaigns not among project {project_id} precursors: {unknown}"
            )
        project.selected_precursor_campaign_ids = cleaned
        project.selected_precursor_campaign_id = cleaned[0]
        self.store.save_project(project)
        logger.info(
            "project %s: %d precursor(s) multi-selected at ARV (lead=%s)",
            project_id, len(cleaned), cleaned[0],
        )
        return project

    def rework_p1(
        self,
        project_id: str,
        new_brief: Brief | None = None,
        new_hyperparams: ProjectHyperparams | None = None,
    ) -> Project:
        """ARV→P1 reverse: user wants a re-run with optional new brief/hyperparams.

        Always starts a fresh P1 batch (new scout + workers). Old precursors
        are cleared.
        """
        project = self._require(project_id)
        if project.phase != Phase.ARV:
            raise ValueError(
                f"rework_p1 only valid in ARV phase (is {project.phase.value})"
            )
        if new_brief is not None:
            project.brief = new_brief
        if new_hyperparams is not None:
            project.hyperparams = new_hyperparams
        project.precursors = []
        project.selected_precursor_campaign_id = None
        project.phase = Phase.P1
        self.store.save_project(project)
        logger.info("project %s: rework P1 (precursors cleared)", project_id)
        return self.run_p1_batch(project)

    # Rework targets allowed per current state (macro-state-machine reverse
    # arrows; see HAA下一步开发汇总.md §2.3). Key: "moribund:<phase>" or "<phase>".
    VALID_REWORK_TARGETS: dict[str, set[str]] = {
        "moribund:p1": {"p1"},
        "moribund:p2": {"p2", "arv", "p1"},
        "arv": {"p1"},
        "ea": {"p2", "arv", "p1"},
    }

    def rework_targets(self, project: Project) -> list[str]:
        """Rework targets available from the project's current state (sorted)."""
        key = (
            f"moribund:{project.phase.value}" if project.is_moribund
            else project.phase.value
        )
        return sorted(self.VALID_REWORK_TARGETS.get(key, set()))

    def rework(
        self,
        project_id: str,
        target: str,
        *,
        new_brief: Brief | None = None,
        new_hyperparams: ProjectHyperparams | None = None,
    ) -> Project:
        """Generic user-driven reverse transition (macro state machine).

        Allowed arrows (per design doc §2.3):
        - MORIBUND(p1) → P1            (fix brief/hyperparams, re-run)
        - MORIBUND(p2) → P2 | ARV | P1
        - ARV → P1                     (no satisfactory precursor)
        - EA   → P2 | ARV | P1         (fix code / swap precursor / full redo)

        Recovering from MORIBUND clears the moribund state; the recovery
        warning is implicit in the UI's confirm dialog (user judgement
        takes precedence over the agent's diagnostic, per the spec).
        """
        project = self._require(project_id)
        allowed = self.rework_targets(project)
        if target not in allowed:
            raise ValueError(
                f"rework to '{target}' not allowed from "
                f"{project.status.value}/{project.phase.value} (allowed: {sorted(allowed)})"
            )
        if new_brief is not None:
            project.brief = new_brief
        if new_hyperparams is not None:
            project.hyperparams = new_hyperparams
        if project.is_moribund:
            project.status = ProjectStatus.IN_PROGRESS
            project.moribund_reason = ""
            project.moribund_diagnostic = ""
        if target == "p1":
            project.precursors = []
            project.selected_precursor_campaign_id = None
            project.phase = Phase.P1
            self.store.save_project(project)
            logger.info("project %s: rework → P1 (precursors cleared)", project_id)
            return self.run_p1_batch(project)
        if target == "arv":
            project.phase = Phase.ARV
            self.store.save_project(project)
            logger.info("project %s: rework → ARV", project_id)
            return project
        if target == "p2":
            if not project.selected_precursor_campaign_id:
                raise ValueError("rework to P2 requires a selected precursor")
            project.phase = Phase.P2
            self.store.save_project(project)
            logger.info("project %s: rework → P2 (re-running experiments)", project_id)
            return self._run_p2_batch(project)
        raise ValueError(f"unknown rework target '{target}'")

    # ------------------------------------------------------------------ #
    #  MORIBUND handling
    # ------------------------------------------------------------------ #

    def set_moribund(self, project: Project, reason: str) -> Project:
        """Set the project to MORIBUND with an LLM-generated diagnostic.

        Collects death reasons from all linked campaigns' dead candidates,
        runs the p1_diagnostic prompt, and records the result.
        """
        death_reasons = self._collect_death_reasons(project.id)
        diagnostic = self._run_p1_diagnostic(project, death_reasons)

        project.moribund_reason = reason
        project.moribund_diagnostic = diagnostic
        project.moribund_history.append(
            MoribundEntry(
                phase=project.phase.value,
                reason=reason,
                diagnostic=diagnostic,
            )
        )
        project.status = ProjectStatus.MORIBUND
        self.store.save_project(project)
        return project

    def recover_from_moribund(
        self, project_id: str, *, confirm_warning: bool = False
    ) -> Project:
        """User-driven MORIBUND→IN_PROGRESS recovery.

        Per the spec: 'allow retry with warning'. Requires explicit confirmation
        so the user acknowledges the previous failure.
        """
        project = self._require(project_id)
        if project.status != ProjectStatus.MORIBUND:
            raise ValueError(
                f"project {project_id} not MORIBUND (is {project.status.value})"
            )
        if not confirm_warning:
            raise ValueError(
                "Recovery requires confirm_warning=True. "
                f"Previous MORIBUND reason: {project.moribund_reason}"
            )
        project.status = ProjectStatus.IN_PROGRESS
        project.moribund_reason = ""
        project.moribund_diagnostic = ""
        self.store.save_project(project)
        logger.info("project %s recovered from MORIBUND", project_id)
        return project

    # ------------------------------------------------------------------ #
    #  Terminal transitions (user-only)
    # ------------------------------------------------------------------ #

    def complete_project(self, project_id: str) -> Project:
        """Mark COMPLETED (only valid post-P3; v0.4 this is a stub)."""
        project = self._require(project_id)
        if project.phase != Phase.DONE:
            raise ValueError(
                f"complete_project only valid when phase=DONE (is {project.phase.value})"
            )
        project.status = ProjectStatus.COMPLETED
        self.store.save_project(project)
        return project

    def abort_project(self, project_id: str, reason: str = "") -> Project:
        """User-only terminal transition. Allowed from any non-terminal state."""
        project = self._require(project_id)
        if project.is_terminal:
            raise ValueError(f"project already terminal: {project.status.value}")
        project.status = ProjectStatus.ABORTED
        self.store.save_project(project)
        logger.info("project %s ABORTED (%s)", project_id, reason)
        return project

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _resolve_pipeline(self, project: Project):
        """Get the Pipeline for this project's P1 batch.

        If a pipeline was injected (testing), use it as-is. Otherwise build a
        fresh Pipeline with the project's hyperparams applied to PipelineConfig.
        """
        if self._pipeline is not None:
            return self._pipeline
        scout_config = replace(
            self.config,
            pipeline=replace(
                self.config.pipeline,
                seek_candidate_count=project.hyperparams.seek_base_count,
                output_candidate_count=project.hyperparams.output_candidate_count,
                max_design_rounds=project.hyperparams.max_design_rounds,
                max_exp_rounds=project.hyperparams.max_exp_rounds,
                max_review_rounds=project.hyperparams.max_review_rounds,
            ),
        )
        return Pipeline(scout_config, self.store, self.budget, llm=self.llm)

    @staticmethod
    def _make_worker_brief(scout_brief: Brief) -> Brief:
        """Clone the scout brief with skip_to='novelty' for a worker Campaign.

        v1.0.2 改用 model_copy：逐字段克隆会在 Brief 增加新字段时静默丢字段
        （knowledge_files 正是这么丢的）。
        """
        return scout_brief.model_copy(update={"skip_to": "novelty"})

    @staticmethod
    def _clone_candidate(src: Candidate, dest_campaign_id: str) -> Candidate:
        """Clone a scout candidate for a worker campaign.

        Resets all per-campaign round counters; keeps the SEEK-level content
        (significance/win_odds/difficulty, claims, rationale).
        """
        return Candidate(
            campaign_id=dest_campaign_id,
            slug=src.slug,
            title=src.title,
            significance=src.significance,
            win_odds=src.win_odds,
            difficulty=src.difficulty,
            rationale=src.rationale,
            positive_claim=src.positive_claim,
            negative_claim=src.negative_claim,
            attack_plan=src.attack_plan,
            closest_prior_work=src.closest_prior_work,
            queue_index=0,
            status=CandidateStatus.PROPOSED,
        )

    def _collect_death_reasons(self, project_id: str) -> list[dict[str, Any]]:
        """Walk every linked Campaign; classify each dead candidate's killer.

        Used by the MORIBUND diagnostic to explain why P1 produced no precursors.
        """
        linked = self.store.list_campaigns_for_project(project_id)
        reasons: list[dict[str, Any]] = []
        for campaign_id, role in linked:
            snap = self.store.restore(campaign_id)
            if snap is None:
                continue
            for cand in snap.candidates:
                if cand.status == CandidateStatus.PUBLISHED:
                    continue  # this one survived
                entry: dict[str, Any] = {
                    "campaign_id": campaign_id,
                    "role": role,
                    "candidate_slug": cand.slug,
                    "candidate_title": cand.title,
                    "final_status": cand.status.value,
                    "grade": cand.grade.value if cand.grade else None,
                }
                if cand.status == CandidateStatus.FILTERED:
                    entry["killed_at"] = "SEEK"
                    entry["reason"] = "filtered_by_output_candidate_count_cap"
                elif cand.grade in (GradeVerdict.TRIVIAL, GradeVerdict.LOOPHOLE):
                    entry["killed_at"] = "GRADE"
                    entry["reason"] = f"grade:{cand.grade.value}"
                elif cand.status == CandidateStatus.DEAD and cand.grade is None:
                    entry["killed_at"] = "NOVELTY_or_SCREEN_or_EXP_FEASIBILITY"
                    entry["reason"] = "dead_before_grade"
                else:
                    entry["killed_at"] = "unknown"
                    entry["reason"] = "unclassified"
                reasons.append(entry)

            # Campaign-level termination reason (if RETIRED).
            if snap.campaign.status == CampaignStatus.RETIRED:
                cp = snap.checkpoint
                if cp and cp.stage.startswith("RETIRED:"):
                    reasons.append({
                        "campaign_id": campaign_id,
                        "role": role,
                        "candidate_slug": None,
                        "candidate_title": None,
                        "final_status": "campaign_retired",
                        "killed_at": "campaign",
                        "reason": cp.stage[len("RETIRED:"):],
                    })
        return reasons

    def _run_p1_diagnostic(
        self, project: Project, death_reasons: list[dict[str, Any]]
    ) -> str:
        """Run the p1_diagnostic prompt through the LLM.

        Returns the diagnostic text. If the LLM call fails, returns a fallback
        message so the MORIBUND transition still completes.
        """
        if not death_reasons:
            return "No candidates were produced or all died without traceable reasons."
        try:
            prompt = render_prompt(
                "p1_diagnostic",
                brief=project.brief,
                death_reasons=death_reasons,
                hyperparams=project.hyperparams,
            )
            resp = self.llm.call(
                messages=[{"role": "user", "content": prompt}],
                stage="P1_DIAGNOSTIC",
            )
            return resp.content
        except Exception as exc:
            logger.warning("P1 diagnostic LLM call failed: %s", exc)
            reasons_str = "; ".join(
                f"{r.get('candidate_title', r.get('reason', '?'))}@{r.get('killed_at', '?')}"
                for r in death_reasons
            )
            return f"(LLM diagnostic unavailable) Death reasons: {reasons_str}"
