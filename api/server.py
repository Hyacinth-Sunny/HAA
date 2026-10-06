"""FastAPI server for HAA — REST surface over the pipeline.

Endpoints
---------
* ``POST   /api/campaigns``           — create a campaign from a Brief
* ``GET    /api/campaigns``           — list all campaigns
* ``GET    /api/campaigns/{id}``      — campaign detail
* ``POST   /api/campaigns/{id}/run``  — run the campaign to a terminal state
* ``GET    /api/campaigns/{id}/report`` — run report (status, candidates, trace)
* ``GET    /api/stats``               — aggregate store stats

The Brief is persisted next to the campaign (``campaigns_dir/<id>/brief.json``)
so the ``run`` endpoint can re-seed SEEK without the caller resubmitting it.

``create_app(config=None)`` is a factory so tests can inject a temp config (and
a temp DB); the module-level ``app`` is the production instance for
``uvicorn api.server:app``.

Note: ``run`` drives the pipeline **synchronously** and will make real LLM
calls. A production deployment should dispatch it to a worker / BackgroundTask;
the skeleton keeps it inline for simplicity.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from haa.budget import BudgetManager
from haa.config import Config, load_config
from haa.models import Brief, Phase, ProjectHyperparams
from haa.observability import setup_logging
from haa.pipeline import Pipeline
from haa.project_controller import ProjectController
from haa.state import StateStore


def _save_brief(campaigns_dir: Path, campaign_id: str, brief: Brief) -> None:
    p = campaigns_dir / campaign_id / "brief.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(brief.model_dump_json(), encoding="utf-8")


def _load_brief(campaigns_dir: Path, campaign_id: str) -> Brief | None:
    p = campaigns_dir / campaign_id / "brief.json"
    if not p.exists():
        return None
    try:
        return Brief.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def create_app(config: Config | None = None) -> FastAPI:
    """Build a FastAPI app wired to a StateStore/BudgetManager/Pipeline."""
    config = config or load_config()
    setup_logging(config)  # ensure per-call token/cost logs are visible
    store = StateStore(config.storage.resolved_db_path())
    budget = BudgetManager(store, global_limit=config.budget.global_limit)
    campaigns_dir = config.storage.resolved_campaigns_dir()
    campaigns_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Hyacinth Automated Analyzer", version="1.0.6", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.budget = budget
    app.state.campaigns_dir = campaigns_dir

    # --- Phase 4b: Jinja2 templates + static files + in-flight run guard ---
    templates_dir = config.project_root / "frontend" / "templates"
    static_dir = config.project_root / "frontend" / "static"
    templates = Jinja2Templates(directory=str(templates_dir))

    def _status_class(status: str) -> str:
        s = (status or "").lower()
        if s == "published":
            return "badge-published"
        if s == "retired":
            return "badge-retired"
        if s in ("queued",) or s.endswith("ing"):
            return "badge-running" if s != "queued" else "badge-other"
        return "badge-other"

    def _phase_class(phase: str) -> str:
        p = (phase or "").lower()
        mapping = {
            "pr": "badge-gray",
            "p1": "badge-blue",
            "arv": "badge-yellow",
            "p2": "badge-blue",
            "ea": "badge-yellow",
            "p3": "badge-blue",
            "done": "badge-green",
        }
        return mapping.get(p, "badge-other")

    def _project_status_class(status: str) -> str:
        s = (status or "").lower()
        mapping = {
            "not_started": "badge-blue",
            "in_progress": "badge-yellow",
            "moribund": "badge-yellow",
            "completed": "badge-green",
            "aborted": "badge-gray",
        }
        return mapping.get(s, "badge-other")

    def _event_class(event_type: str) -> str:
        t = (event_type or "").lower()
        if t == "llm_call":
            return "badge-blue"
        if "trunc" in t:
            return "badge-red"
        if "measure" in t:
            return "badge-green"
        return "badge-other"

    def _money(v: Any) -> str:
        try:
            return f"${float(v):.2f}"
        except (TypeError, ValueError):
            return "—"

    def _dt(v: Any) -> str:
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M")
        return str(v)[:16] if v else ""

    def _dtime(v: Any) -> str:
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return str(v)[:19] if v else ""

    templates.env.filters["status_class"] = _status_class

    def _md(text: str) -> str:
        """Render Markdown to safe HTML（论文/判决/简报等 LLM 产出的 md 正文）.

        v1.0.2 前端修复：① 返回 Markup——Jinja 自动转义会把生成的 HTML 当
        文本显示（此前 moribund 诊断的渲染一直是坏的）；② 剥 <script>/<style>/
        on* 属性——LLM 产出按不可信内容处理。
        """
        if not text:
            return ""
        try:
            import markdown as _markdown
            from markupsafe import Markup
            import re as _re

            html_out = _markdown.markdown(
                str(text), extensions=["tables", "fenced_code", "nl2br"]
            )
            html_out = _re.sub(
                r"<(script|style|iframe)[^>]*>.*?</\1>", "", html_out,
                flags=_re.S | _re.I,
            )
            html_out = _re.sub(r'\son\w+="[^"]*"', "", html_out, flags=_re.I)
            return Markup(html_out)
        except Exception:
            import html as _html
            from markupsafe import Markup

            return Markup("<pre>" + _html.escape(str(text)) + "</pre>")

    templates.env.filters["md"] = _md
    templates.env.filters["phase_class"] = _phase_class
    templates.env.filters["project_status_class"] = _project_status_class
    templates.env.filters["event_class"] = _event_class
    templates.env.filters["money"] = _money
    templates.env.filters["dt"] = _dt
    templates.env.filters["dtime"] = _dtime
    templates.env.filters["short_id"] = lambda i: str(i)[:12]

    app.state.templates = templates
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    # Guard against double-running the same campaign via the async run endpoint.
    app.state.running = set()
    app.state.running_lock = threading.Lock()
    # Projects currently executing a long-running controller operation (P1/P2/P3).
    app.state.running_projects = set()

    def _lang(request: Request) -> str:
        lang = request.query_params.get("lang", "zh")
        return lang if lang in ("zh", "en") else "zh"

    def _page_ctx(request: Request, active: str, **extra: Any) -> dict:
        ctx: dict[str, Any] = {"lang": _lang(request), "active": active}
        ctx.update(extra)
        return ctx

    def _controller() -> ProjectController:
        return ProjectController(config, store, budget)

    def _stage_summary(cp) -> dict[str, Any]:
        """Verdict chip + expandable detail blocks for one checkpoint row.

        Returns {"chip", "cls", "detail" (one-line), "full" ([{label, text}…])}
        so the timeline stays compact and the user clicks 详情 for full text.
        """
        stage = cp.stage or ""
        s = stage.split(":")[0]
        ctx = cp.context or {}
        chip, detail, cls = "", "", "chip-gray"
        full: list[dict[str, str]] = []

        def _blk(label: str, text: Any, limit: int = 4000) -> None:
            t = str(text or "").strip()
            if t:
                full.append({"label": label, "text": t[:limit]})

        try:
            if s == "SEEK" and ctx.get("candidates"):
                cands = ctx["candidates"]
                n = len(cands)
                chip = f"{n} cand"
                detail = " | ".join(str(c.get("title", ""))[:40] for c in cands[:3])
                cls = "chip-blue"
                for c in cands[:8]:
                    if isinstance(c, dict):
                        _blk(
                            str(c.get("title", "?")),
                            f"EV={float(c.get('significance', 0)) * float(c.get('win_odds', 0)):.2f} "
                            f"[{c.get('status', '?')}] {c.get('rationale', '')}",
                            600,
                        )
            elif s == "NOVELTY" and isinstance(ctx.get("novelty"), dict):
                nov = ctx["novelty"]
                v = str(nov.get("verdict", "?"))
                chip = v
                detail = str(nov.get("rationale", ""))[:150]
                cls = {"NEW": "chip-green", "SOLVED": "chip-red"}.get(v, "chip-yellow")
                cw = str(nov.get("closest_prior_work", "") or "")
                if v != "NEW" and cw and cw != "none":
                    detail = f"最近工作: {cw[:80]} — {detail}"
                _blk("verdict / rationale", f"{v}\n\n{nov.get('rationale', '')}")
                _blk("closest_prior_work", nov.get("closest_prior_work"))
                _blk("search_queries_used", nov.get("search_queries_used"))
            elif s == "SCREEN" and isinstance(ctx.get("screen"), dict):
                sc = ctx["screen"]
                surv = sc.get("survives")
                if surv:
                    chip = "放行"
                    cls = "chip-green"
                else:
                    chip = f"杀:{sc.get('kill_method', '?')}"
                    cls = "chip-red"
                detail = str(sc.get("rationale", ""))[:150]
                _blk("proposition", sc.get("proposition"))
                _blk("separating_instance", sc.get("separating_instance"))
                _blk("kill_method / evidence", f"{sc.get('kill_method', '')}\n{sc.get('evidence', '')}")
                _blk("rationale", sc.get("rationale"))
            elif s == "VERIFY":
                vp = ctx.get("verify_passed")
                finds = ctx.get("verify_findings") or []
                if vp:
                    chip, cls = "PASS 无反例", "chip-green"
                else:
                    chip, cls = f"反例×{len(finds)}", "chip-red"
                if finds and isinstance(finds[0], dict):
                    detail = str(finds[0].get("detail", ""))[:150]
                for i, f in enumerate(finds[:5], 1):
                    if isinstance(f, dict):
                        _blk(f"反例{i}", f.get("detail"))
                    else:
                        _blk(f"反例{i}", f)
                extra_v = (ctx.get("extra") or {}).get("verify") or {}
                for k2, v2 in (extra_v.items() if isinstance(extra_v, dict) else []):
                    _blk(k2, v2, 1200)
            elif s == "GRADE" and isinstance(ctx.get("grade"), dict):
                g = ctx["grade"]
                chip = str(g.get("grade", "?"))
                detail = str(g.get("rationale", ""))[:150]
                cls = {"solid": "chip-green", "thin": "chip-yellow",
                       "trivial": "chip-red", "loophole": "chip-red"}.get(chip, "chip-gray")
                _blk("grade / rationale", f"{chip}\n\n{g.get('rationale', '')}")
            elif s == "EXP_FEASIBILITY":
                blockers = (ctx.get("extra") or {}).get("exp_findings") or []
                if blockers:
                    chip, cls = f"{len(blockers)} blockers", "chip-yellow"
                else:
                    chip, cls = "PASS", "chip-green"
                for i, b in enumerate(blockers[:6], 1):
                    if isinstance(b, dict):
                        _blk(
                            f"blocker{i} [{b.get('severity')}/{b.get('category')}]",
                            f"{b.get('detail', '')}\n\nevidence: {b.get('evidence', '')}\n\nfix: {b.get('fix_suggestion', '')}",
                        )
            elif s == "WRITE" and isinstance(ctx.get("paper"), dict):
                chip, cls = f"{len(ctx['paper'])} 章节", "chip-blue"
                for sec, txt in (ctx["paper"] or {}).items():
                    _blk(sec, txt, 2000)
            elif s == "REVIEW" and isinstance(ctx.get("review"), dict):
                r = ctx["review"]
                d = str(r.get("decision", "?"))
                ov = r.get("overall")
                chip = f"{d} {ov}" if ov is not None else d
                cls = "chip-green" if d == "accept" else "chip-yellow"
                scores = r.get("scores") or {}
                _blk("scores", json.dumps(scores, ensure_ascii=False, indent=1))
                reports = r.get("reports") or {}
                if isinstance(reports, dict):
                    for lens, rep in reports.items():
                        _blk(f"评审·{lens}", rep)
                else:
                    _blk("reports", reports)
            elif stage == "PUBLISHED":
                chip, cls = "✓ 前体产出", "chip-green"
            elif stage.startswith("RETIRED"):
                chip, cls = "✗ 退役", "chip-red"
                detail = stage[8:]
            kills = (ctx.get("extra") or {}).get("kills") or []
            if kills:
                k = kills[-1]
                v = k.get("verdict", {})
                chip = f"✗ {k.get('stage', '?')}杀 {k.get('slug', '')[:18]}"
                cls = "chip-red"
                ev = v.get("evidence") or v.get("rationale") or ""
                detail = f"{v.get('kill_method', k.get('reason', ''))}: {str(ev)[:120]}"
                blockers = v.get("blockers") or []
                if blockers:
                    for i, b in enumerate(blockers[:4], 1):
                        if isinstance(b, dict):
                            _blk(
                                f"kill·blocker{i} [{b.get('severity')}/{b.get('category')}]",
                                f"{b.get('detail', '')}\n\nfix: {b.get('fix_suggestion', '')}",
                            )
                else:
                    _blk("kill 证据", f"{v.get('kill_method', '')}\n{ev}")
                    _blk("kill rationale", v.get("rationale"))
            elif s == "SEEK":
                filtered = sum(
                    1 for c in ctx.get("candidates", [])
                    if isinstance(c, dict) and c.get("status") == "filtered"
                )
                if filtered:
                    chip, cls = f"cap: {filtered} filtered", "chip-yellow"
        except Exception:
            pass
        return {"chip": chip, "detail": detail, "cls": cls, "full": full}

    def _candidate_dossiers(project_id: str) -> list[dict[str, Any]]:
        """Per-candidate dossier: full SEEK content + every stage verdict it
        received, gathered from all linked campaigns' checkpoints.

        This is what lets the user see WHAT each idea was (including rejected
        ones — rationale, paired claims, attack plan) and WHY each stage
        judged it the way it did (NOVELTY/SCREEN/VERIFY/GRADE + kills).
        """
        dossiers: dict[str, dict[str, Any]] = {}
        linked = store.list_campaigns_for_project(project_id)
        for cid, role in linked:
            for cp in store.list_checkpoints(cid):
                ctx = cp.context or {}
                st = cp.stage or ""
                # SEEK:done carries the full candidate list (incl. filtered/dead).
                if st.startswith("SEEK") and st.endswith(":done"):
                    for c in ctx.get("candidates") or []:
                        if not isinstance(c, dict) or not c.get("slug"):
                            continue
                        d = dossiers.setdefault(c["slug"], {
                            "slug": c["slug"],
                            "candidate": c,
                            "verdicts": {},
                            "kills": [],
                            "campaign_role": role,
                        })
                        # Keep the richest snapshot (later checkpoints may have
                        # status updates but SEEK:done has the full claims).
                        d["candidate"] = c
                # Stage verdicts attach to the candidate active at that moment.
                active = (ctx.get("candidate") or {}).get("slug")
                if not active:
                    continue
                d = dossiers.setdefault(active, {
                    "slug": active, "candidate": ctx.get("candidate") or {},
                    "verdicts": {}, "kills": [], "campaign_role": role,
                })
                base = st.split(":")[0]
                if st.endswith(":done"):
                    if base == "NOVELTY" and ctx.get("novelty"):
                        d["verdicts"]["NOVELTY"] = ctx["novelty"]
                    elif base == "SCREEN" and ctx.get("screen"):
                        d["verdicts"]["SCREEN"] = ctx["screen"]
                    elif base == "GRADE" and ctx.get("grade"):
                        d["verdicts"]["GRADE"] = ctx["grade"]
                    elif base == "VERIFY":
                        d["verdicts"].setdefault("VERIFY", []).append({
                            "passed": bool(ctx.get("verify_passed")),
                            "findings": ctx.get("verify_findings") or [],
                        })
                    elif base == "EXP_FEASIBILITY":
                        d["verdicts"].setdefault("EXP_FEASIBILITY", []).append({
                            "blockers": (ctx.get("extra") or {}).get("exp_findings") or [],
                        })
                    elif base == "REVIEW" and isinstance(ctx.get("review"), dict):
                        d["verdicts"]["REVIEW"] = ctx["review"]
                    elif base == "WRITE" and isinstance(ctx.get("paper"), dict):
                        d["verdicts"]["PAPER_SECTIONS"] = sorted(ctx["paper"].keys())
                for k in (ctx.get("extra") or {}).get("kills") or []:
                    entry = dict(k)
                    if entry.get("slug") == active:
                        d["kills"].append(entry)
        # Latest VERIFY/EXP entry per candidate = last list element.
        out = list(dossiers.values())
        for d in out:
            for key in ("VERIFY", "EXP_FEASIBILITY"):
                v = d["verdicts"].get(key)
                if isinstance(v, list) and v:
                    d["verdicts"][key] = v[-1]
        # Sort: active first, then by status, then slug.
        order = {"active": 0, "proposed": 1, "dead": 2, "filtered": 3, "published": 4}
        out.sort(key=lambda d: (order.get(d["candidate"].get("status", ""), 9), d["slug"]))
        return out

    def _project_context(project_id: str) -> dict[str, Any] | None:
        """Assemble the full template context for a project detail page."""
        p = store.get_project(project_id)
        if p is None:
            return None
        linked = store.list_campaigns_for_project(project_id)
        campaigns = []
        for cid, role in linked:
            c = store.get_campaign(cid)
            if c:
                campaigns.append({"role": role, "campaign": c})
        candidate_rows = []
        for cid, _ in linked:
            for cand in store.list_candidates(cid):
                candidate_rows.append({"campaign_id": cid, "candidate": cand})
        checkpoints_by_campaign = {
            cid: store.list_checkpoints(cid) for cid, _ in linked
        }
        cp_summaries = {}
        for cps in checkpoints_by_campaign.values():
            for cp in cps:
                cp_summaries[cp.seq] = _stage_summary(cp)
        events = store.list_events()
        return {
            "project": p,
            "linked": campaigns,
            "candidate_rows": candidate_rows,
            "checkpoints_by_campaign": checkpoints_by_campaign,
            "cp_summaries": cp_summaries,
            "events": events,
            "candidate_dossiers": _candidate_dossiers(project_id),
            "rework_targets": _controller().rework_targets(p),
            "running_now": project_id in app.state.running_projects,
        }

    def _run_project_op(project_id: str, op: str, background_tasks: BackgroundTasks):
        """Dispatch a long-running project operation to a background thread.

        Returns True if a new task was started (False if already running).
        Uses the same WAL + RLock store that the campaign runner relies on:
        the background writer and the polling HTTP readers don't block each
        other.
        """
        with app.state.running_lock:
            if project_id in app.state.running_projects:
                return False
            app.state.running_projects.add(project_id)

        def _run():
            try:
                ctrl = _controller()
                if op == "start":
                    ctrl.start_project(project_id)
                elif op == "advance":
                    p = store.get_project(project_id)
                    if p is None:
                        return
                    if p.phase == Phase.ARV:
                        ctrl.advance_to_p2(project_id)
                    elif p.phase == Phase.EA:
                        ctrl.advance_to_p3(project_id)
                elif op.startswith("rework:"):
                    ctrl.rework(project_id, op.split(":", 1)[1])
                elif op == "rework_p1":
                    ctrl.rework_p1(project_id)
            except Exception:
                pass  # project state reflects whatever happened
            finally:
                with app.state.running_lock:
                    app.state.running_projects.discard(project_id)

        background_tasks.add_task(_run)
        return True

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- Projects API（外框架，只读） ------------------------------------- #
    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return [json.loads(p.model_dump_json()) for p in store.list_projects()]

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        linked = store.list_campaigns_for_project(project_id)
        campaigns = []
        for cid, role in linked:
            c = store.get_campaign(cid)
            if c:
                campaigns.append({"role": role, "campaign": json.loads(c.model_dump_json())})
        return {"project": json.loads(p.model_dump_json()), "linked_campaigns": campaigns}

    @app.post("/api/projects")
    def create_project_api(brief: Brief, hyperparams: ProjectHyperparams | None = None) -> dict[str, Any]:
        """Create a Project in PR phase (from JSON body)."""
        p = _controller().create_project(brief, hyperparams)
        return json.loads(p.model_dump_json())

    # --- Events / Logs API（可观测性，只读） ------------------------------ #
    @app.get("/api/events")
    def list_events(campaign_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        events = store.list_events(campaign_id)
        return [json.loads(e.model_dump_json()) for e in events[-limit:]]

    @app.get("/api/projects/{project_id}/events")
    def list_project_events(project_id: str, limit: int = 500) -> list[dict[str, Any]]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        linked = store.list_campaigns_for_project(project_id)
        all_events = []
        for cid, _ in linked:
            all_events.extend(store.list_events(cid))
        all_events.sort(key=lambda e: e.seq)
        return [json.loads(e.model_dump_json()) for e in all_events[-limit:]]

    # --- Config API（设置页只读展示，脱敏） ------------------------------- #
    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        cfg = app.state.config
        return {
            "llm": {"model": cfg.llm.model, "api_key_env": cfg.llm.api_key_env},
            "budget": {
                "per_campaign": cfg.budget.per_campaign,
                "global_limit": cfg.budget.global_limit,
            },
            "pipeline": {
                "seek_candidate_count": cfg.pipeline.seek_candidate_count,
                "output_candidate_count": cfg.pipeline.output_candidate_count,
                "max_design_rounds": cfg.pipeline.max_design_rounds,
                "max_review_rounds": cfg.pipeline.max_review_rounds,
                "max_exp_rounds": cfg.pipeline.max_exp_rounds,
            },
            "timeouts": {"llm": cfg.timeouts.llm, "stage": cfg.timeouts.stage},
            "logging": {"level": cfg.logging.level, "format": cfg.logging.format},
            "storage": {"db_path": cfg.storage.db_path, "campaigns_dir": cfg.storage.campaigns_dir},
            "acp": {
                "acpx_path": cfg.acp.acpx_path,
                "timeout_simple": cfg.acp.timeout_simple,
                "timeout_complex": cfg.acp.timeout_complex,
                "timeout_module": cfg.acp.timeout_module,
                "fallback_to_agentloop": cfg.acp.fallback_to_agentloop,
            },
            "tools": {
                "exec_bash": {
                    "timeout": cfg.tools.exec_bash.timeout,
                    "blocked_commands": list(cfg.tools.exec_bash.blocked_commands),
                },
                "file_ops": {
                    "allowed_paths": list(cfg.tools.file_ops.allowed_paths),
                    "max_file_size_mb": cfg.tools.file_ops.max_file_size_mb,
                },
                "web_fetch": {
                    "timeout": cfg.tools.web_fetch.timeout,
                    "max_chars": cfg.tools.web_fetch.max_chars,
                },
                "web_search": {
                    "max_results": cfg.tools.web_search.max_results,
                },
            },
            "ssh": {"host": cfg.ssh.host, "user": cfg.ssh.user},
        }

    @app.post("/api/config")
    def save_config(updates: dict[str, Any]) -> dict[str, Any]:
        """Save config changes to the YAML file and hot-reload.

        Accepts flat dot-notation keys: {"llm.model": "gpt-4o", "budget.per_campaign": 5.0}
        Writes to the resolved config YAML, then reloads app.state.config.
        Changes take effect for NEW operations (not in-progress runs).
        """
        import yaml as _yaml
        from haa.config import resolve_config_path

        config_path = resolve_config_path()
        try:
            with open(config_path, encoding="utf-8") as f:
                raw = _yaml.safe_load(f) or {}
        except FileNotFoundError:
            raw = {}

        # Apply flat dot-notation updates.
        changed = []
        for key, value in updates.items():
            parts = key.split(".")
            d = raw
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
            changed.append(key)

        # Write back (preserves structure; comments are lost — acceptable for web edits).
        with open(config_path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Hot-reload config in memory.
        new_config = load_config(config_path)
        app.state.config = new_config
        return {"status": "ok", "changed": changed}

    # --- Prompts API（设置页高级——查看系统提示词，只读） ------------------ #
    @app.get("/api/prompts")
    def list_prompts() -> list[str]:
        from haa.prompts import prompt_names
        return prompt_names()

    @app.get("/api/prompts/{name:path}")
    def get_prompt(name: str) -> dict[str, Any]:
        from pathlib import Path
        p = config.project_root / "prompts" / f"{name}.md"
        if not p.exists():
            raise HTTPException(status_code=404, detail="prompt not found")
        return {"name": name, "content": p.read_text(encoding="utf-8")}

    # --- Project 生命周期写端点（操作触发） ------------------------------- #
    @app.post("/api/projects/{project_id}/start")
    def project_start(project_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        _run_project_op(project_id, "start", background_tasks)
        return json.loads(store.get_project(project_id).model_dump_json())

    @app.post("/api/projects/{project_id}/advance")
    def project_advance(project_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        _run_project_op(project_id, "advance", background_tasks)
        return json.loads(store.get_project(project_id).model_dump_json())

    @app.post("/api/projects/{project_id}/select")
    def project_select(project_id: str, campaign_id: str = Form(...)) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            result = _controller().select_precursor(project_id, campaign_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return json.loads(result.model_dump_json())

    @app.post("/api/projects/{project_id}/abort")
    def project_abort(project_id: str) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            result = _controller().abort_project(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return json.loads(result.model_dump_json())

    @app.post("/api/projects/{project_id}/recover")
    def project_recover(project_id: str, confirm: bool = Form(True)) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            result = _controller().recover_from_moribund(project_id, confirm_warning=confirm)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return json.loads(result.model_dump_json())

    @app.post("/api/projects/{project_id}/complete")
    def project_complete(project_id: str) -> dict[str, Any]:
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            result = _controller().complete_project(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return json.loads(result.model_dump_json())

    @app.post("/api/projects/{project_id}/rework")
    def project_rework(
        project_id: str,
        background_tasks: BackgroundTasks,
        target: str = Form("p1"),
    ) -> dict[str, Any]:
        """Generic user-driven rework (target: p1 | p2 | arv). Long targets
        (p1/p2) run in the background; arv is a quick transition."""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        allowed = _controller().rework_targets(p)
        if target not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"rework to '{target}' not allowed from "
                f"{p.status.value}/{p.phase.value} (allowed: {sorted(allowed)})",
            )
        if target == "arv":
            _controller().rework(project_id, target)
        else:
            _run_project_op(project_id, f"rework:{target}", background_tasks)
        return json.loads(store.get_project(project_id).model_dump_json())

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, Any]:
        """Delete a terminal project (COMPLETED/ABORTED) and all linked data."""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not p.is_terminal:
            raise HTTPException(
                status_code=409,
                detail=f"cannot delete non-terminal project (status={p.status.value})"
            )
        store.delete_project(project_id)
        return {"status": "deleted", "project_id": project_id}

    @app.post("/api/projects/{project_id}/brief")
    def update_brief(project_id: str, brief: Brief) -> dict[str, Any]:
        """Update a project's brief (only allowed in PR phase)."""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        if p.phase != Phase.PR:
            raise HTTPException(
                status_code=409,
                detail=f"brief can only be edited in PR phase (current={p.phase.value})"
            )
        p.brief = brief
        store.save_project(p)
        return json.loads(p.model_dump_json())

    @app.post("/api/campaigns")
    def create_campaign(brief: Brief) -> dict[str, Any]:
        campaign = store.create_campaign(brief, budget_limit=config.budget.per_campaign)
        _save_brief(campaigns_dir, campaign.id, brief)
        return json.loads(campaign.model_dump_json())

    @app.get("/api/campaigns")
    def list_campaigns() -> list[dict[str, Any]]:
        return [json.loads(c.model_dump_json()) for c in store.list_campaigns()]

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str) -> dict[str, Any]:
        c = store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return json.loads(c.model_dump_json())

    @app.post("/api/campaigns/{campaign_id}/run")
    def run_campaign(campaign_id: str) -> dict[str, Any]:
        c = store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        if c.is_terminal:
            raise HTTPException(
                status_code=409, detail=f"campaign already {c.status.value}"
            )
        brief = _load_brief(campaigns_dir, campaign_id)
        pipeline = Pipeline(config, store, budget)
        campaign = pipeline.run_campaign(campaign_id, brief=brief)
        return json.loads(campaign.model_dump_json())

    @app.get("/api/campaigns/{campaign_id}/report")
    def report(campaign_id: str) -> dict[str, Any]:
        snap = store.restore(campaign_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        checkpoints = store.list_checkpoints(campaign_id)
        return {
            "campaign": json.loads(snap.campaign.model_dump_json()),
            "candidates": [json.loads(c.model_dump_json()) for c in snap.candidates],
            "budget": {
                "used": snap.campaign.budget_used,
                "limit": snap.campaign.budget_limit,
                "remaining": snap.campaign.budget_remaining,
            },
            "checkpoint_count": len(checkpoints),
            "latest_stage": checkpoints[-1].stage if checkpoints else None,
            "latest_context": checkpoints[-1].context if checkpoints else {},
        }

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return store.stats()

    @app.get("/api/tool-stats")
    def tool_stats(campaign_id: str | None = None) -> dict[str, Any]:
        """工具调用统计（v1.0.6-rev2）：per-tool 调用数/失败率/耗时/阶段分布。

        stage_tool_limits 调参的数据源——撞帽看 by_stage，质量看 failure_rate。"""
        from haa.observability import tool_call_stats

        return tool_call_stats(store, campaign_id)

    # ------------------------------------------------------------------ #
    #  Phase 4b: HTML pages + HTMX partials
    # ------------------------------------------------------------------ #
    def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
        # Starlette >=0.29 TemplateResponse(request, name, context) signature.
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def _paper_checkpoints(campaign_id: str):
        cps = store.list_checkpoints(campaign_id)
        return [c for c in cps if (c.context or {}).get("paper")]

    # --- Five-page navigation: Home / Projects / Logs / Settings / About ---
    @app.get("/", response_class=HTMLResponse)
    def page_home(request: Request):
        return _render(request, "pages/home.html", _page_ctx(
            request, "home",
            projects=store.list_projects(),
            campaigns=store.list_campaigns(),
            stats=store.stats(),
            events=store.list_events()[-8:],
        ))

    @app.get("/projects", response_class=HTMLResponse)
    def page_projects(request: Request):
        return _render(request, "pages/projects.html", _page_ctx(
            request, "projects",
            projects=store.list_projects(),
        ))

    @app.get("/projects/new", response_class=HTMLResponse)
    def page_new_project(request: Request):
        return _render(request, "pages/project_new.html", _page_ctx(request, "projects"))

    @app.post("/projects/upload-brief", response_class=HTMLResponse)
    async def upload_brief_form(request: Request, file: UploadFile = File(...)):
        """上传原版格式研究简报（.md）→ 编译 → 预填表单供审阅（不直接生效）。

        v1.0.2 简报编译器：原版 Markdown 经 LLM 抽取五字段 + knowledge_files，
        用户在表单里核对/修改后才提交创建——编译不直接建项目。
        """
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        try:
            from haa.brief_compiler import compile_brief

            controller = _controller()
            brief = compile_brief(text, controller.llm)
        except Exception as exc:  # noqa: BLE001 — 编译失败回表单报错
            return _render(request, "pages/project_new.html", _page_ctx(
                request, "projects",
                error=f"简报编译失败：{exc}",
            ), status_code=422)
        return _render(request, "pages/project_new.html", _page_ctx(
            request, "projects",
            title=brief.title,
            problem_area=brief.problem_area,
            track=brief.track.value,
            constraints="\n".join(brief.constraints),
            exclusions="\n".join(brief.exclusions),
            knowledge_files="\n".join(brief.knowledge_files),
            compiled_notice="已从上传的原版简报编译——请核对（约束应为原文逐字）后提交",
        ))

    @app.post("/projects", response_class=HTMLResponse)
    def create_project_form(
        request: Request,
        title: str = Form(...),
        problem_area: str = Form(...),
        track: str = Form("theory"),
        constraints: str = Form(""),
        exclusions: str = Form(""),
        knowledge_files: str = Form(""),
        seek_base_count: int = Form(5),
        output_candidate_count: int = Form(3),
    ):
        try:
            brief = Brief(
                title=title.strip(),
                problem_area=problem_area.strip(),
                track=track,
                constraints=[ln for ln in constraints.splitlines() if ln.strip()],
                exclusions=[ln for ln in exclusions.splitlines() if ln.strip()],
                knowledge_files=[ln for ln in knowledge_files.splitlines() if ln.strip()],
            )
            hp = ProjectHyperparams(
                seek_base_count=seek_base_count,
                output_candidate_count=output_candidate_count,
            )
        except ValidationError as exc:
            return _render(request, "pages/project_new.html", _page_ctx(
                request, "projects",
                error=str(exc), title=title, problem_area=problem_area,
                track=track, constraints=constraints, exclusions=exclusions,
                knowledge_files=knowledge_files,
                seek_base_count=seek_base_count,
                output_candidate_count=output_candidate_count,
            ), status_code=422)
        project = _controller().create_project(brief, hp)
        return RedirectResponse(url=f"/projects/{project.id}", status_code=303)

    @app.post("/projects/{project_id}/delete", response_class=HTMLResponse)
    def delete_project_form(project_id: str):
        """Delete a terminal project via form POST, redirect to /projects."""
        p = store.get_project(project_id)
        if p is None or not p.is_terminal:
            raise HTTPException(status_code=409, detail="cannot delete non-terminal project")
        store.delete_project(project_id)
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/{project_id}/rework", response_class=HTMLResponse)
    def rework_project_form(
        project_id: str,
        background_tasks: BackgroundTasks,
        target: str = Form("p1"),
    ):
        """HTML-form rework: recover MORIBUND / ARV / EA to a chosen phase."""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        allowed = _controller().rework_targets(p)
        if target not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"rework to '{target}' not allowed (allowed: {sorted(allowed)})",
            )
        if target == "arv":
            _controller().rework(project_id, target)
        else:
            _run_project_op(project_id, f"rework:{target}", background_tasks)
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/brief", response_class=HTMLResponse)
    def update_brief_form(
        request: Request,
        project_id: str,
        title: str = Form(...),
        problem_area: str = Form(...),
        track: str = Form("theory"),
        constraints: str = Form(""),
        exclusions: str = Form(""),
    ):
        """Update brief via form POST (only in PR phase)."""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        if p.phase != Phase.PR:
            raise HTTPException(status_code=409, detail="brief only editable in PR phase")
        try:
            p.brief = Brief(
                title=title.strip(),
                problem_area=problem_area.strip(),
                track=track,
                constraints=[ln for ln in constraints.splitlines() if ln.strip()],
                exclusions=[ln for ln in exclusions.splitlines() if ln.strip()],
            )
        except ValidationError as exc:
            ctx = _project_context(project_id)
            ctx["brief_error"] = str(exc)
            return _render(request, "pages/project_detail.html", _page_ctx(
                request, "projects", **ctx,
            ), status_code=422)
        store.save_project(p)
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/select", response_class=HTMLResponse)
    async def select_precursors_form(request: Request, project_id: str):
        """ARV 多选前体（v1.0.2 修复：此前卡片表单提交到本路径但路由不存在，
        连单选都 404）。勾选多个 → 首个为下游消费的主前体。

        手动解析 form：FastAPI 的 ``list[str] = Form()`` 对 urlencoded 重复键
        解析有已知怪癖（422），``getlist`` 两条路径都稳。
        """
        form = await request.form()
        campaign_ids = list(form.getlist("campaign_ids"))
        if not campaign_ids:
            raise HTTPException(status_code=422, detail="no precursor selected")
        try:
            _controller().select_precursors(project_id, campaign_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.get("/projects/{project_id}/precursors/{campaign_id}/download")
    def download_precursor(project_id: str, campaign_id: str):
        """下载论文前体为 Markdown 文件（v1.0.2：ARV 此前无法导出）。"""
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="project not found")
        pre = next(
            (x for x in p.precursors if x.campaign_id == campaign_id), None
        )
        if pre is None:
            raise HTTPException(
                status_code=404,
                detail=f"campaign {campaign_id} not among precursors",
            )
        paper = pre.paper or {}
        lines: list[str] = [f"# {paper.get('title') or pre.candidate_title}", ""]
        lines.append(
            f"> grade={pre.grade} · campaign={campaign_id}"
            f" · degraded={bool(paper.get('degraded'))}"
        )
        lines.append("")
        for sec in ("abstract", "intro", "background", "method", "eval", "related", "conclusion"):
            body = (paper.get(sec) or "").strip()
            if body:
                lines += [f"## {sec.upper()}", body, ""]
        review = pre.review or {}
        if review:
            lines += ["## REVIEW", f"overall: {review.get('overall')}", ""]
            for lens, rep in (review.get("reports") or {}).items():
                if isinstance(rep, dict):
                    lines += [
                        f"### {lens} (score {rep.get('score')})",
                        str(rep.get("verdict") or ""), "",
                    ]
        spec = pre.exp_spec or {}
        if spec:
            lines += ["## EXPERIMENT DESIGN"]
            # v1.0.5 修复：exp_spec 实际 schema 是 experiments/code_spec/
            # literature_reference/addresses_exp_findings（旧键清单全空导致
            # 附录节空壳）。trace 除外全部序列化。
            for k, v in spec.items():
                if k == "trace" or v in (None, "", [], {}):
                    continue
                if isinstance(v, (list, dict)):
                    lines += [f"**{k}:**", "```json", json.dumps(
                        v, ensure_ascii=False, indent=1, default=str)[:20000], "```", ""]
                else:
                    lines += [f"**{k}:** {v}", ""]
        slug = pre.candidate_slug or campaign_id[:8]
        from urllib.parse import quote

        return Response(
            "\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(slug)}.md"
                )
            },
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def page_project(request: Request, project_id: str):
        ctx = _project_context(project_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render(request, "pages/project_detail.html", _page_ctx(
            request, "projects", **ctx,
        ))

    @app.get("/logs", response_class=HTMLResponse)
    def page_logs(request: Request, project_id: str | None = None, event_type: str | None = None):
        projects = store.list_projects()
        project_name = {}
        for p in projects:
            project_name[p.id] = p.brief.title
            for cid, _ in store.list_campaigns_for_project(p.id):
                project_name[cid] = p.brief.title
        events = store.list_events()
        if project_id:
            events = [e for e in events if e.campaign_id == project_id]
        if event_type and event_type != "all":
            events = [e for e in events if e.event_type == event_type]
        events = events[-200:]
        events.reverse()
        event_types = sorted({e.event_type for e in store.list_events()})
        return _render(request, "pages/logs.html", _page_ctx(
            request, "logs",
            projects=projects,
            events=events,
            project_name=project_name,
            event_types=event_types,
            filter_project=project_id or "",
            filter_type=event_type or "",
        ))

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request):
        from haa.prompts import prompt_names
        cfg = app.state.config
        return _render(request, "pages/settings.html", _page_ctx(
            request, "settings",
            config=cfg,
            prompt_names=prompt_names(),
            hp_defaults=ProjectHyperparams(),
        ))

    @app.get("/about", response_class=HTMLResponse)
    def page_about(request: Request):
        """说明书页：内联速览 + 渲染仓库根的 HAA项目说明书.md（单一来源，v1.0.5）。"""
        manual_md = ""
        manual_path = config.project_root / "HAA项目说明书.md"
        if manual_path.exists():
            try:
                manual_md = manual_path.read_text(encoding="utf-8")
            except OSError:
                manual_md = ""
        return _render(
            request, "pages/about.html",
            _page_ctx(request, "about", manual_md=manual_md),
        )

    # --- HTMX partials for the five-page UI ---
    @app.get("/partials/projects", response_class=HTMLResponse)
    def partial_projects(request: Request):
        return _render(request, "partials/project_rows.html", _page_ctx(
            request, "projects", projects=store.list_projects(),
        ))

    @app.get("/partials/projects/{project_id}/status", response_class=HTMLResponse)
    def partial_project_status(request: Request, project_id: str):
        ctx = _project_context(project_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render(request, "partials/project_status.html", _page_ctx(
            request, "projects", **ctx,
        ))

    @app.get("/partials/projects/{project_id}/progress", response_class=HTMLResponse)
    def partial_project_progress(request: Request, project_id: str):
        ctx = _project_context(project_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render(request, "partials/project_progress.html", _page_ctx(
            request, "projects", **ctx,
        ))

    @app.get("/partials/projects/{project_id}/precursors", response_class=HTMLResponse)
    def partial_project_precursors(request: Request, project_id: str):
        ctx = _project_context(project_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render(request, "partials/precursor_cards.html", _page_ctx(
            request, "projects", **ctx,
        ))

    @app.get("/partials/logs", response_class=HTMLResponse)
    def partial_logs(request: Request, project_id: str | None = None, event_type: str | None = None):
        projects = store.list_projects()
        project_name = {}
        for p in projects:
            project_name[p.id] = p.brief.title
            for cid, _ in store.list_campaigns_for_project(p.id):
                project_name[cid] = p.brief.title
        events = store.list_events()
        if project_id:
            events = [e for e in events if e.campaign_id == project_id]
        if event_type and event_type != "all":
            events = [e for e in events if e.event_type == event_type]
        events = events[-200:]
        events.reverse()
        return _render(request, "partials/log_rows.html", _page_ctx(
            request, "logs", events=events, project_name=project_name,
            filter_project=project_id or "", filter_type=event_type or "",
        ))

    @app.get("/partials/prompts/{name:path}", response_class=HTMLResponse)
    def partial_prompt(request: Request, name: str):
        from pathlib import Path
        p = config.project_root / "prompts" / f"{name}.md"
        if not p.exists():
            raise HTTPException(status_code=404, detail="prompt not found")
        return _render(request, "partials/prompt_viewer.html", _page_ctx(
            request, "settings",
            prompt={"name": name, "content": p.read_text(encoding="utf-8")},
        ))

    @app.get("/campaigns/new", response_class=HTMLResponse)
    def page_new_campaign(request: Request):
        return _render(request, "pages/brief_editor.html", _page_ctx(request, "home"))

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    def page_campaign(request: Request, campaign_id: str):
        snap = store.restore(campaign_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        checkpoints = store.list_checkpoints(campaign_id)
        return _render(request, "pages/campaign_detail.html", _page_ctx(request, "home", **{
            "campaign": snap.campaign,
            "candidates": snap.candidates,
            "checkpoints": checkpoints,
            "latest": checkpoints[-1] if checkpoints else None,
            "running_now": campaign_id in app.state.running,
        }))

    @app.get("/campaigns/{campaign_id}/paper", response_class=HTMLResponse)
    def page_paper(request: Request, campaign_id: str, seq: int | None = None):
        snap = store.restore(campaign_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        paper_cps = _paper_checkpoints(campaign_id)
        current = next((c for c in paper_cps if c.seq == seq), paper_cps[-1] if paper_cps else None)
        return _render(request, "pages/paper_viewer.html", _page_ctx(request, "home", **{
            "campaign": snap.campaign,
            "paper": current.context.get("paper") if current else None,
            "paper_checkpoints": paper_cps,
            "current_seq": current.seq if current else None,
        }))

    @app.post("/campaigns", response_class=HTMLResponse)
    def create_campaign_form(
        request: Request,
        title: str = Form(...),
        problem_area: str = Form(...),
        track: str = Form("theory"),
        constraints: str = Form(""),
        exclusions: str = Form(""),
    ):
        try:
            brief = Brief(
                title=title.strip(),
                problem_area=problem_area.strip(),
                track=track,
                constraints=[ln for ln in constraints.splitlines() if ln.strip()],
                exclusions=[ln for ln in exclusions.splitlines() if ln.strip()],
            )
        except ValidationError as exc:
            return _render(request, "pages/brief_editor.html", _page_ctx(request, "home", **{
                "error": str(exc), "title": title, "problem_area": problem_area,
                "track": track, "constraints": constraints, "exclusions": exclusions,
            }), status_code=422)
        campaign = store.create_campaign(brief, budget_limit=config.budget.per_campaign)
        _save_brief(campaigns_dir, campaign.id, brief)
        return RedirectResponse(url=f"/campaigns/{campaign.id}", status_code=303)

    @app.post("/campaigns/{campaign_id}/run", response_class=HTMLResponse)
    def run_campaign_async(request: Request, campaign_id: str, background_tasks: BackgroundTasks):
        """Async run: hand the pipeline to a background thread, return a status
        partial immediately. The store is thread-safe (WAL + RLock) so the
        background writer and the polling HTTP reader don't block each other."""
        c = store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        latest = store.latest_checkpoint(campaign_id)
        if c.is_terminal:
            return _render(request, "partials/campaign_status.html",
                           {"campaign": c, "latest": latest, "running_now": False})
        with app.state.running_lock:
            already = campaign_id in app.state.running
            if not already:
                app.state.running.add(campaign_id)

        def _run():
            try:
                brief = _load_brief(campaigns_dir, campaign_id)
                Pipeline(config, store, budget).run_campaign(campaign_id, brief=brief)
            except Exception:
                pass  # status/checkpoints already reflect what happened
            finally:
                with app.state.running_lock:
                    app.state.running.discard(campaign_id)

        if not already:
            background_tasks.add_task(_run)
        refreshed = store.get_campaign(campaign_id)
        return _render(request, "partials/campaign_status.html",
                       {"campaign": refreshed, "latest": latest, "running_now": True})

    # --- HTMX partials ---
    @app.get("/partials/stats", response_class=HTMLResponse)
    def partial_stats(request: Request):
        return _render(request, "partials/stats.html", {"stats": store.stats()})

    @app.get("/partials/campaigns", response_class=HTMLResponse)
    def partial_campaigns(request: Request):
        return _render(request, "partials/campaign_rows.html", {"campaigns": store.list_campaigns()})

    @app.get("/partials/campaigns/{campaign_id}/status", response_class=HTMLResponse)
    def partial_status(request: Request, campaign_id: str):
        c = store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return _render(request, "partials/campaign_status.html", {
            "campaign": c, "latest": store.latest_checkpoint(campaign_id),
            "running_now": campaign_id in app.state.running,
        })

    @app.get("/partials/campaigns/{campaign_id}/progress", response_class=HTMLResponse)
    def partial_progress(request: Request, campaign_id: str):
        c = store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return _render(request, "partials/progress_timeline.html", {
            "campaign": c, "checkpoints": store.list_checkpoints(campaign_id),
        })

    @app.get("/partials/campaigns/{campaign_id}/candidates", response_class=HTMLResponse)
    def partial_candidates(request: Request, campaign_id: str):
        return _render(request, "partials/candidate_queue.html",
                       {"candidates": store.list_candidates(campaign_id)})

    @app.get("/partials/campaigns/{campaign_id}/paper", response_class=HTMLResponse)
    def partial_paper(request: Request, campaign_id: str, seq: int | None = None):
        paper_cps = _paper_checkpoints(campaign_id)
        current = next((c for c in paper_cps if c.seq == seq), paper_cps[-1] if paper_cps else None)
        return _render(request, "partials/paper_sections.html",
                       {"paper": current.context.get("paper") if current else None})

    return app


# Production instance. Start with either:
#   python -m api.server                              # reads config server.host/port (0.0.0.0:8420)
#   python -m uvicorn api.server:app --port 8420      # explicit
# Note: bare `uvicorn api.server:app` defaults to port 8000, NOT 8420 — and a
# system uvicorn outside the project env will fail to import fastapi/litellm.
app = create_app()


if __name__ == "__main__":
    # `python -m api.server` → run on the configured host/port, using this
    # env's own uvicorn. No need to remember --port, no dependency on a system
    # uvicorn binary. API keys are still read from env vars at request time.
    import uvicorn

    _cfg = load_config()
    print(f"HAA starting → http://{_cfg.server.host}:{_cfg.server.port}")
    uvicorn.run(app, host=_cfg.server.host, port=_cfg.server.port)
