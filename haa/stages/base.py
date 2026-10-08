"""Stage base classes and the shared result/context types.

A **stage** is one bounded unit of work that makes a single (retried) LLM call
and returns a :class:`StageResult`. The pipeline (haa/pipeline.py) owns all
control flow — a stage never decides the *next* stage on its own authority; it
returns a status + findings, and the pipeline's transition rules resolve what
runs next. This is HAA's core philosophy: *the model doesn't know which step
it's on; code does.*

Types
-----
* :class:`StageStatus` — the four things a stage can ask for.
* :class:`StageResult` — a stage's return value.
* :class:`StageContext` — the working memory threaded between stages and
  persisted in every checkpoint (HM-Pro Lesson 1: rework must not be blind).
* :class:`BaseStage` — abstract ``run(campaign, context) -> StageResult``.

Note on naming: the task brief calls the inter-stage carrier ``CampaignContext``;
the pre-existing ``haa/stages/__init__.py`` imports it as ``StageContext``. It
is one concept, so there is one class (``StageContext``); ``haa/pipeline.py``
re-exports it as ``CampaignContext`` for callers that prefer that name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from haa.models import Brief, Campaign, Candidate


class StageStatus(str, Enum):
    """What a stage asks the pipeline to do next.

    * ``CONTINUE``        — success; advance (the pipeline picks the next stage).
    * ``RETRY``           — re-run *this* stage (an intra-stage loop; rare).
    * ``ABORT_CANDIDATE`` — the current candidate is dead (HM-Pro Lesson 4):
                           archive it and advance the queue.
    * ``ABORT_CAMPAIGN``  — unrecoverable; retire the whole campaign.
    """

    CONTINUE = "continue"
    RETRY = "retry"
    ABORT_CANDIDATE = "abort_candidate"
    ABORT_CAMPAIGN = "abort_campaign"


@dataclass
class StageResult:
    """A stage's return value.

    ``data`` carries the stage's structured output (design artifact, grade,
    paper draft, …) which the pipeline folds into the :class:`StageContext`.
    ``findings`` is a list of structured observations (e.g. verify
    counterexamples, review scores). ``next_stage`` is an *advisory* hint — the
    pipeline's transition rules are the source of truth and may override it.
    """

    status: StageStatus = StageStatus.CONTINUE
    data: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    next_stage: str | None = None
    # For ABORT_CANDIDATE / queue mutations a stage may hand back the updated
    # candidate so the pipeline can persist it without re-fetching.
    candidate: Candidate | None = None

    @classmethod
    def continue_(cls, **data: Any) -> "StageResult":
        return cls(status=StageStatus.CONTINUE, data=dict(data))

    @classmethod
    def abort_candidate(cls, reason: str = "", **data: Any) -> "StageResult":
        return cls(
            status=StageStatus.ABORT_CANDIDATE,
            data={"reason": reason, **data},
        )

    @classmethod
    def abort_campaign(cls, reason: str = "", **data: Any) -> "StageResult":
        return cls(
            status=StageStatus.ABORT_CAMPAIGN,
            data={"reason": reason, **data},
        )


@dataclass
class StageContext:
    """Working memory threaded between stages — the crash-recovery payload.

    Every field here is what a *later* stage needs that an *earlier* stage
    produced. It is serialised into each checkpoint so that, after a crash, the
    pipeline resumes with full memory (HM-Pro Lesson 1: VERIFY→DESIGN rework
    that forgot ``verify_findings`` did three blind iterations).

    The current :attr:`candidate` and the :attr:`candidates` queue are carried
    here for convenience; the StateStore also persists them independently.
    """

    # Inputs.
    brief: Brief | None = None

    # The candidate currently flowing through DESIGN→…→REFINE, plus the
    # full ordered queue (HM-Pro Lesson 4: dead candidates advance the queue).
    candidate: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    current_candidate_id: str | None = None

    # --- Per-stage outputs (folded in by the pipeline) -------------------
    novelty: dict[str, Any] = field(default_factory=dict)
    screen: dict[str, Any] = field(default_factory=dict)
    design: dict[str, Any] = field(default_factory=dict)

    # Lesson 1: VERIFY findings MUST survive into DESIGN on rework.
    verify_findings: list[dict[str, Any]] = field(default_factory=list)
    verify_passed: bool = False

    grade: dict[str, Any] = field(default_factory=dict)
    paper: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)

    # Lesson 3: the best REVIEW snapshot so far; REFINE rolls back to it if a
    # round degrades quality.
    best_snapshot: dict[str, Any] | None = None

    # Round counters (mirrored on the Candidate; kept here for fast access).
    design_round: int = 0
    review_round: int = 0
    # Phase A: experiment-design loop counters.
    exp_round: int = 0                    # EXP_SPEC⇄EXP_FEASIBILITY inner loop
    exp_to_design_round: int = 0          # EXP→DESIGN outer loop

    # Free-form escape hatch for stage-specific data not worth a field.
    extra: dict[str, Any] = field(default_factory=dict)

    # --- (de)serialisation for checkpoints --------------------------------
    def to_checkpoint_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for StateStore.save_checkpoint."""
        d: dict[str, Any] = {}
        if self.brief is not None:
            d["brief"] = self.brief.model_dump(mode="json")
        if self.candidate is not None:
            d["candidate"] = self.candidate.model_dump(mode="json")
        if self.candidates:
            d["candidates"] = [c.model_dump(mode="json") for c in self.candidates]
        if self.current_candidate_id is not None:
            d["current_candidate_id"] = self.current_candidate_id
        for key in (
            "novelty",
            "screen",
            "design",
            "verify_findings",
            "grade",
            "paper",
            "review",
            "extra",
        ):
            value = getattr(self, key)
            if value:
                d[key] = value
        if self.verify_passed:
            d["verify_passed"] = True
        if self.best_snapshot is not None:
            d["best_snapshot"] = self.best_snapshot
        d["design_round"] = self.design_round
        d["review_round"] = self.review_round
        d["exp_round"] = self.exp_round
        d["exp_to_design_round"] = self.exp_to_design_round
        return d

    @classmethod
    def from_checkpoint(cls, d: dict[str, Any]) -> "StageContext":
        """Rebuild a context from a checkpoint dict (inverse of above)."""
        ctx = cls()
        if d.get("brief"):
            ctx.brief = Brief.model_validate(d["brief"])
        if d.get("candidate"):
            ctx.candidate = Candidate.model_validate(d["candidate"])
        if d.get("candidates"):
            ctx.candidates = [Candidate.model_validate(c) for c in d["candidates"]]
        ctx.current_candidate_id = d.get("current_candidate_id")
        for key in (
            "novelty",
            "screen",
            "design",
            "verify_findings",
            "grade",
            "paper",
            "review",
            "extra",
        ):
            if key in d:
                setattr(ctx, key, d[key])
        ctx.verify_passed = bool(d.get("verify_passed", False))
        ctx.best_snapshot = d.get("best_snapshot")
        ctx.design_round = int(d.get("design_round", 0))
        ctx.review_round = int(d.get("review_round", 0))
        ctx.exp_round = int(d.get("exp_round", 0))
        ctx.exp_to_design_round = int(d.get("exp_to_design_round", 0))
        return ctx

    # --- convenience ------------------------------------------------------
    def snapshot_paper(self) -> dict[str, Any]:
        """Capture the current paper + review state (for Lesson 3 rollback)."""
        return {"paper": _deep_copy(self.paper), "review": _deep_copy(self.review)}

    def restore_paper(self, snap: dict[str, Any]) -> None:
        self.paper = _deep_copy(snap.get("paper", {}))
        self.review = _deep_copy(snap.get("review", {}))


def _deep_copy(value: Any) -> Any:
    """JSON-round-trip copy; everything in a context is JSON-shaped."""
    import json

    if not value:
        return value
    return json.loads(json.dumps(value, default=str))


class BaseStage:
    """Abstract base for all pipeline stages.

    Subclasses set ``name`` and implement :meth:`run`. The ``timeout`` class
    attribute is a per-stage watchdog (HM-Pro Lesson 7); the pipeline overrides
    it at runtime with ``config.timeouts.stage`` when one is configured.
    """

    name: str = "base"
    timeout: float = 300.0  # task default; overridden by config.timeouts.stage

    # Tools this stage may invoke via the agent loop (Phase 2). ``None`` (the
    # default) means "no tool access declared" — stages that want tools list the
    # tool names here, and the ToolRegistry still enforces its own per-stage
    # allowlist as the source of truth. This is an advisory declaration a stage
    # can use to narrow (never widen) the registry's offer.
    allowed_tools: set[str] | None = None

    def __init__(self, llm: Any = None, config: Any = None):
        self.llm = llm
        self.config = config
        # Last agent-loop metadata (truncated/iterations/cost); set by
        # _run_agent, read by Pipeline._observe (Phase 6a-fronted).
        self._last_meta: dict[str, Any] | None = None

    def run(self, campaign: Campaign, context: StageContext) -> StageResult:  # noqa: D401
        """Execute the stage. Subclasses must override."""
        raise NotImplementedError(f"{type(self).__name__}.run() not implemented")

    # -- Phase 3 helpers (tool-using stages) ---------------------------------

    def _make_agent_loop(self) -> Any:
        """Build the stage's tool-using agent loop.

        大修 M0 起：执行引擎切换为新 Harness（``haa/harness/agent_loop.py``）。
        旧 ``haa/llm/tools.ToolRegistry`` 仍负责构造 16 工具的行为本体（沙箱/
        黑名单/SSRF 闸全在旧 handler 内原样生效），随后经
        ``haa.harness.tools_bridge.registry_from_legacy`` **换壳**进新注册表——
        注册格式换成四件套、执行通道换成统一检查层（先记账→路径白名单→
        执行→写闸门→结果处理→冻结记账）与事件日志（tool/call、tool/result
        ＋旧名镜像，tool-stats 不中断）。

        对 stage 代码完全透明：循环签名与返回结构同旧 AgentLoop，
        ``stage_name``=``self.name`` 的菜单过滤照旧（config/tool_menu.yaml
        声明式覆盖优先）。
        """
        from haa.harness.agent_loop import AgentLoop
        from haa.harness.registry import ToolMenu
        from haa.harness.tools.native import apply_native_tools
        from haa.harness.tools_bridge import registry_from_legacy
        from haa.llm.tools import ToolRegistry

        registry_client = self._sub_agent_client()
        if self.config is not None:
            campaigns_dir = self.config.storage.resolved_campaigns_dir()
            legacy_registry = ToolRegistry(
                self.config.tools, campaigns_dir=campaigns_dir, client=registry_client,
                vision_config=self.config.vision,
            )
            menu = ToolMenu.load(self.config.harness.tool_menu)
            write_gate = bool(self.config.harness.write_gate_enabled)
            ssh_profiles = dict(self.config.harness.ssh_servers)
        else:
            legacy_registry = ToolRegistry(campaigns_dir="data/campaigns", client=registry_client)
            menu = ToolMenu(None)
            write_gate = False
            ssh_profiles = {}
        harness_registry = registry_from_legacy(
            legacy_registry,
            event_sink=getattr(self.llm, "event_sink", None),
            write_gate_enabled=write_gate,
            menu=menu,
        )
        # M1 原生接管：五个底层工具的原生实现替换旧 handler（模型可见
        # 契约不变），新增 job/ssh/监控五件；命令黑名单挂入检查链。
        native_services = apply_native_tools(
            harness_registry,
            allowed_roots=(legacy_registry.campaigns_dir,),
            ssh_profiles=ssh_profiles,
        )
        native_services.budget_ref = lambda: getattr(self.llm, "budget", None)
        loop = AgentLoop(self.llm, harness_registry, event_sink=getattr(self.llm, "event_sink", None))
        if self.config is not None:
            loop.tool_result_max_chars = self.config.tools.agent_result_max_chars
            loop.read_file_result_max_chars = self.config.tools.read_file_result_max_chars
        return loop

    def _sub_agent_client(self) -> Any:
        """Client handed to the tool registry for ``multi_agents`` sub-agents.

        Returns the primary client unless ``llm.sub_agent_model`` names a
        different (typically lighter) model; then builds a client reusing the
        primary's credentials, timeout, and budget manager so sub-agent spend
        stays on the same campaign budget.
        """
        sub_model = ""
        if self.config is not None:
            sub_model = getattr(self.config.llm, "sub_agent_model", "") or ""
        if not sub_model:
            return self.llm
        try:
            import os as _os

            from haa.llm.client import LLMClient

            main = self.config.llm
            api_key = _os.environ.get(main.api_key_env) or None
            return LLMClient(
                model=sub_model,
                timeout=self.config.timeouts.llm,
                wall_timeout=self.config.timeouts.llm_wall,
                max_retries=main.max_retries,
                api_base=main.api_base or None,
                api_key=api_key,
                provider_options=main.provider_options,
                pricing=main.pricing,
                budget=getattr(self.llm, "budget", None),
            )
        except Exception:
            return self.llm

    def _run_agent(
        self,
        prompt: str,
        *,
        campaign_id: str = "",
        stage_name: str = "",
        max_tool_calls: int = 10,
        json_mode: bool = False,
    ) -> Any:
        """Run the agent loop and return its ``AgentLoopResult``.

        Thin convenience wrapper so stages don't repeat the make+run boilerplate.
        Pair with :meth:`_agent_meta` to surface truncation/cost signals to the
        pipeline (Phase 6a-fronted observability).
        """
        agent = self._make_agent_loop()
        # stage 墙钟（v1.0.2 复活 timeouts.stage——此前是死配置）：到点按
        # 工具帽同款路径优雅收尾（truncated=True，仍出判决）。
        stage_deadline = None
        if self.config is not None:
            stage_deadline = float(self.config.timeouts.stage)
        result = agent.run(
            prompt,
            stage_name=stage_name,
            campaign_id=campaign_id,
            max_tool_calls=max_tool_calls,
            json_mode=json_mode,
            deadline_s=stage_deadline,
        )
        self._last_meta = self._agent_meta(result)  # pipeline reads this via _observe
        return result

    def _tool_limit(self, default: int) -> int:
        """Per-stage tool-call cap: YAML override wins over the built-in default.

        Lets ``pipeline.stage_tool_limits.<STAGE>`` in config override any
        stage's hardcoded cap without code changes (smoke audit: caps tuned
        for light models truncated 19× under v4-pro). ``default`` is each
        stage's own baked-in value.
        """
        if self.config is not None:
            overrides = getattr(self.config.pipeline, "stage_tool_limits", None) or {}
            v = overrides.get(self.name.upper())
            if isinstance(v, int) and v >= 0:
                return v
        return default

    def _brief_block(self, brief: Any) -> str:
        """简报铁律块（v1.0.3 指令遵循修复）。

        smoke5 实证：brief 只送达 SEEK/NOVELTY，DESIGN 起的 8 个阶段从未
        见过简报 → DESIGN 凭候选标题自由发挥，把简报架构整个偷换
        （16 研究包被实体化为 16 节点、双算/SDC 检测消失），三路审不审
        简报符合性 → 漂移一路绿灯。现在每个阶段 prompt 尾部统一注入：
        约束**逐字** + 排除方向 + 知识清单。
        """
        if brief is None:
            return ""
        lines = [
            "",
            "⛓⛓⛓ 简报铁律（研究简报的硬约束——你的产出必须遵守；"
            "违反任何一条即无效产出，会被 fidelity 审查退回）⛓⛓⛓",
        ]
        if getattr(brief, "constraints", None):
            lines.append("硬约束（逐字执行，不得概括性满足）：")
            lines += [f"  - {c}" for c in brief.constraints]
        if getattr(brief, "exclusions", None):
            lines.append("明确不做：")
            lines += [f"  - {e}" for e in brief.exclusions]
        kfs = getattr(brief, "knowledge_files", None) or []
        if kfs:
            from pathlib import Path

            lines.append(
                "研究原料（campaign 沙箱 knowledge/ 下 read_file 可读；"
                "涉及相关组件时必读原文，不得凭记忆另起）："
            )
            lines += [f"  - knowledge/{Path(k).name}" for k in kfs]
        return "\n".join(lines)

    def _memory_brief_suffix(self, brief: Any) -> str:
        """Tombstone-memory suffix for stage prompts (v1.0 memory module).

        Retrieves the cross-project memory brief (dead ideas with failure
        reasons + open directions) for this brief and formats it as an
        explicitly watermarked prompt suffix. Iron rule: tombstones and open
        questions ONLY — never successful patterns (those belong to v1.1's
        repair-side injection). Empty string when memory is disabled or has
        no relevant entries; any failure returns "" (memory is an add-on
        layer and must never break the pipeline).
        """
        if self.config is None or not getattr(self.config.memory, "enabled", False):
            return ""
        try:
            from haa.memory import MemoryStore

            mem = MemoryStore(
                self.config.project_root / self.config.memory.memory_dir,
                max_inject=self.config.memory.injection_max_items,
            )
            return "\n\n" + mem.brief_for(brief)
        except Exception:  # noqa: BLE001 — 记忆层绝不影响主管线
            return ""

    def _anchor(self, context):
        """锚定模式三元组 (anchor, idea_id, enabled)：feature 开关×简报锚点。

        开关默认关（Ch6 特性开关纪律，default.yaml features.anchored_mode）；
        config 为 None（纯单测）时锚点存在即启用。"""
        anchor = getattr(getattr(context, "brief", None), "hypothesis_anchor", None)
        if anchor is None:
            return None, None, False
        enabled = self.config is None or self.config.harness.feature("anchored_mode")
        return (anchor if enabled else None), \
            getattr(context.brief, "anchor_idea_id", None), anchor is not None and enabled

    def _anchor_guard_clause(self) -> str:
        from haa.anchor_guard import ANCHOR_GUARD_CLAUSE

        return ANCHOR_GUARD_CLAUSE

    def _campaign_tomb_block(self, campaign, context) -> str:
        """战役内墓穴死路清单（大修批次2，第二章 §8 墓穴即时版）。

        数据源优先级：进程内 ``context.extra["kills"]``（权威、随 checkpoint
        存活）→ ``artifacts/kills.json`` 文件回退（跨进程 resume 时 extra
        重建前仍拿得到全量死路）。空 → ""（SEEK 第一轮无注入——"第二轮起"
        语义由此天然成立）。逻辑归属楼层 50-99 记忆注入区；M0 楼层化挂账
        清偿前走 prompt 后缀通道。任何失败返回 ""（记忆层不破主管线）。
        """
        try:
            kills = list((getattr(context, "extra", None) or {}).get("kills") or [])
            if not kills and campaign is not None and self.config is not None:
                from haa.memory import load_campaign_kills

                campaigns_dir = self.config.storage.resolved_campaigns_dir(
                    self.config.project_root
                )
                kills = load_campaign_kills(campaigns_dir, campaign.id)
            if not kills:
                return ""
            from haa.memory import campaign_tomb_block

            block = campaign_tomb_block(kills)
            return ("\n\n" + block) if block else ""
        except Exception:  # noqa: BLE001 — 记忆层绝不影响主管线
            return ""

    @staticmethod
    def _agent_meta(result: Any) -> dict:
        """Extract observability metadata from an ``AgentLoopResult``.

        Stages attach this to ``StageResult.data["_meta"]``; the pipeline reads
        and pops it (so it never lands in a checkpoint) to detect truncation and
        emit ``stage_truncated`` events.
        """
        return {
            "truncated": bool(getattr(result, "truncated", False)),
            "iterations": int(getattr(result, "iterations", 0)),
            "tool_calls_count": len(getattr(result, "tool_calls", []) or []),
            "total_cost_usd": float(getattr(result, "total_cost_usd", 0.0)),
        }

    @staticmethod
    def _parse_json(content: Any, default: Any = None) -> dict:
        """Parse a JSON object from LLM output text; tolerant of code fences.

        Agent-loop stages ask for JSON via ``json_mode``; the model usually
        complies but may wrap it in ``` ```json ``` fences or add prose. This
        finds the first balanced ``{...}`` and parses it. Returns ``default``
        (defaulting to ``{}``) on any failure.
        """
        import json

        if default is None:
            default = {}
        if not content:
            return default
        if isinstance(content, dict):
            return content
        obj = _extract_json_object(str(content))
        if isinstance(obj, dict):
            return obj
        return default

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"


def _extract_json_object(text: str) -> Any:
    """Return the first balanced ``{...}`` object in ``text`` parsed, else None.

    Scans brace depth while respecting string literals and escapes, so braces
    inside strings don't confuse the matcher.
    """
    import json

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
