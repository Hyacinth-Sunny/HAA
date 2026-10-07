"""Typed configuration loaded from ``config/default.yaml``.

The config is a plain YAML file at the project root; this module turns it into
frozen dataclasses so the rest of the codebase gets attribute access and type
hints instead of ``cfg["llm"]["model"]`` dict spelunking.

Resolution order for which file is loaded:
1. The ``HAA_CONFIG`` env var, if set (absolute path to a YAML file).
2. ``config/default.yaml`` at the project root.

The project root is the parent of the ``haa`` package directory, so this works
regardless of the current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ``haa/config.py`` → parent is ``haa/`` → parent again is the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "default.yaml"


@dataclass(frozen=True)
class LLMConfig:
    """LLM client settings (see haa/llm/client.py)."""

    model: str = "glm-5.2"
    max_retries: int = 2
    api_base: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    sub_agent_model: str = ""  # multi_agents 子代理模型（空 = 复用主模型）
    # Provider 专属请求参数，逐字透传给 litellm（GLM-5.3-flash 等需要）。
    # 例：{tool_stream: true, thinking: {type: enabled}, reasoning_effort: high}
    # —— tool_stream 对 GLM 流式工具调用是硬需求：不传则流式下 tool_calls
    #    不产出，HAA 全流式架构（client.py stream=True）会拿不到工具调用。
    # 若含 "stream_options" 键则覆盖默认的 include_usage。
    provider_options: dict = field(default_factory=dict)
    # 显式价目兜底（USD / 百万 tokens）：{input_per_m, output_per_m}。
    # litellm 价目表不认识的模型（如 glm-5.3-flash）completion_cost 恒 0，
    # 预算闸致盲——配置了此处单价则按真实 token 数自算 cost。
    pricing: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TimeoutsConfig:
    """Three timeout layers (HM-Pro Lesson 7 + smoke4 双闸改造).

    - ``llm``: 流式化后 = 相邻 chunk 间隔上限（空闲闸）。长尾生成持续吐
      字节永不触发；真挂死 300s 无字节即断。
    - ``llm_wall``: 单次调用总墙钟上限。空闲闸管"挂了"，墙钟闸管"生成
      失控"（实证最长合法调用 1041s，1800s 留余量）。
    - ``stage``: 单阶段墙钟（agent-loop 迭代间检查，超时按工具帽截断收尾）。
    """

    llm: int = 300
    llm_wall: int = 1800
    stage: int = 600


@dataclass(frozen=True)
class BudgetConfig:
    """Dual-layer caps (USD); see haa/budget.py."""

    per_campaign: float = 10.0
    global_limit: float = 100.0


@dataclass(frozen=True)
class PipelineConfig:
    """Loop ceilings and SEEK target."""

    max_design_rounds: int = 3
    max_review_rounds: int = 3
    # REVIEW 四路均分的接受线（v1.0.5 配置化——smoke5/6 的 0.667/0.675 死在
    # 硬编码 0.7 上，"差强人意带"三轮空转后照发 degraded）。
    review_accept_threshold: float = 0.7
    seek_candidate_count: int = 5
    max_exp_rounds: int = 3              # EXP_SPEC⇄EXP_FEASIBILITY inner loop（Phase A）
    max_exp_to_design_rounds: int = 1    # EXP→DESIGN outer loop
    output_candidate_count: int = 3      # SEEK 后幸存到 NOVELTY/SCREEN 筛查的候选上限（P1 批量）
    # 每阶段工具调用上限覆盖（键 = 阶段名大写；空 = 用各 stage 内置默认）。
    # 冒烟审计：轻模型校准的硬编码上限在 v4-pro 下 19 次截断（DESIGN 5/5 打满）。
    stage_tool_limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8420


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "data/haa.db"
    campaigns_dir: str = "data/campaigns"

    def resolved_db_path(self, root: Path | None = None) -> Path:
        """Absolute DB path. Relative paths resolve against the project root."""
        p = Path(self.db_path)
        return p if p.is_absolute() else (root or _PROJECT_ROOT) / p

    def resolved_campaigns_dir(self, root: Path | None = None) -> Path:
        p = Path(self.campaigns_dir)
        return p if p.is_absolute() else (root or _PROJECT_ROOT) / p


# --- Tool layer (Phase 2) ----------------------------------------------------
# Settings for the function-calling sandbox in haa/llm/tools.py. Tuples (not
# lists) are used because the dataclasses are frozen.


@dataclass(frozen=True)
class WebSearchConfig:
    """Web search provider (tavily / serpapi / brave)."""

    provider: str = "tavily"
    api_key_env: str = "TAVILY_API_KEY"
    max_results: int = 5


@dataclass(frozen=True)
class WebFetchConfig:
    """URL fetch + text extraction."""

    timeout: int = 30
    max_chars: int = 10000


@dataclass(frozen=True)
class ExecBashConfig:
    """Sandboxed shell execution (HM-Pro Lesson 7: every call times out)."""

    timeout: int = 30
    blocked_commands: tuple[str, ...] = (
        "rm -rf",
        "sudo ",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        ":(){",  # fork bomb
        "nc -l",
        "chmod 777",
    )


@dataclass(frozen=True)
class FileOpsConfig:
    """File read/write sandboxing (path allowlist + size cap)."""

    allowed_paths: tuple[str, ...] = ("data/campaigns/", "data/")
    max_file_size_mb: int = 10


@dataclass(frozen=True)
class SearchPaperConfig:
    """Academic paper search (Semantic Scholar v1 needs no key)."""

    provider: str = "semantic_scholar"
    api_key_env: str = ""
    max_results: int = 10
    # v1.0 回退链：S2 失败/空结果时依次尝试（openalex 无需 key；arXiv 覆盖预印本）。
    fallback_chain: tuple[str, ...] = ("openalex", "arxiv")
    email: str = ""  # OpenAlex polite-pool 标识（可选，填入后更稳定）


@dataclass(frozen=True)
class LocalLibraryConfig:
    """本地论文库检索（opt-in，v1.0.6-rev3）。

    库内论文按刊物目录存放（如 ~/papers/ICML2026/Regular/），无领域分类——
    匹配只看文件名里的标题 token 重合度，不依赖分类。由 OpenClaw 等辅助
    离线采集（OpenReview 指南流程），HAA 只读不写。
    """
    enabled: bool = False          # 默认关：无领域分类，按需显式开启
    dir: str = ""                  # 用户指定的检索根路径（开启后必填）


@dataclass(frozen=True)
class FulltextConfig:
    """论文全文获取（v1.0.6-rev3，haa/llm/fulltext.py）。

    发现链顺序（按本机 NAT 网络实测）：本地库(opt-in) → OpenAlex → arXiv →
    Unpaywall → PMC → S2。逐源容错，任一源失败不炸链。
    """
    email: str = "N5_yzh@163.com"  # Unpaywall polite-pool 标识（须真实格式）
    timeout_s: int = 90            # 单次 PDF 下载读超时（历史 30s 不够，成功案例可到 138s）
    max_mb: int = 30               # 下载大小帽
    retries: int = 2               # 每候选 URL 重试次数（arXiv DNS 间歇实证）
    local_library: LocalLibraryConfig = field(default_factory=LocalLibraryConfig)


@dataclass(frozen=True)
class ToolsConfig:
    """All tool-layer settings (Phase 2)."""

    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    web_fetch: WebFetchConfig = field(default_factory=WebFetchConfig)
    exec_bash: ExecBashConfig = field(default_factory=ExecBashConfig)
    file_ops: FileOpsConfig = field(default_factory=FileOpsConfig)
    search_paper: SearchPaperConfig = field(default_factory=SearchPaperConfig)
    # 循环内上下文压缩（smoke4 复盘：工具结果全文进 messages，DESIGN 峰值
    # 51.7K in，成本随工具数平方涨）。read_file 独立大帽——22KB 级知识文件
    # 必须整读（v2 简报依赖）。
    agent_result_max_chars: int = 8000
    read_file_result_max_chars: int = 30000
    fulltext: FulltextConfig = field(default_factory=FulltextConfig)


@dataclass(frozen=True)
class LoggingConfig:
    """Observability settings (Phase 6a-fronted).

    Without an explicit ``logging`` config, Python's root logger uses the
    lastResort handler → only WARNING+ reaches stderr and every ``logger.info``
    in the LLM client / agent loop is silently dropped. ``setup_logging`` wires
    real handlers so the per-call token/cost lines (and later structured events)
    are actually visible.
    """

    level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    format: str = "text"  # text | json
    output: str = "stdout"  # stdout | file
    file_path: str = "data/haa.log"


@dataclass(frozen=True)
class SSHConfig:
    """SSH 远程执行配置（Phase B / Part II 远程实验）。"""

    host: str = ""
    user: str = ""
    key_path: str = "~/.ssh/id_rsa"
    remote_base_dir: str = "~/haa_runs"
    poll_interval_s: int = 30
    poll_timeout_s: int = 3600
    control_path: str = ""


@dataclass(frozen=True)
class VisionConfig:
    """视觉理解模型配置（论文插图理解，Phase C）。"""

    provider: str = "openai_compatible"
    model: str = "glm-4.6v"
    api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key_env: str = "ZHIPU_API_KEY"
    max_tokens: int = 2000


@dataclass(frozen=True)
class MemoryConfig:
    """跨项目持久记忆配置（v1.0）。

    墓志铭（failed + failure_reason）+ 派生双纸（context_brief /
    open_questions）。注入物只含墓碑与未解问题，绝不注入成功模式。
    """

    enabled: bool = True
    memory_dir: str = "data/memory"       # 相对 project_root
    injection_max_items: int = 8          # 注入 prompt 的墓碑条数上限
    inject_into_novelty: bool = True      # NOVELTY 也注入（默认开）


@dataclass(frozen=True)
class ACPConfig:
    """ACP（Agent Client Protocol）配置——HAA↔Claude Code 编码通道（v0.6）。

    acpx 是 ACP 协议的 CLI 客户端，HAA 通过 subprocess 调用它来委托
    Claude Code 进行代码生成/调试。详见《Coding Agent ACP迁移参考.md》。
    大修 D2（v1.1，2026-10-07）：本通道**停用保留**——模块挪至
    ``haa/fallback/``，``enabled`` 默认 false，不开启时 P2 默认执行路径
    不可达；仅当新 Harness 在编码任务上大声失败且短期无法修复时，
    打开此开关临时降级。
    """

    enabled: bool = False  # D2 停用保留：默认 false（大修第一章 §4.3）
    acpx_path: str = ""  # 空=自动探测（bundled OpenClaw > which > npx）
    session_prefix: str = "haa"  # session 名前缀（实际名= prefix-campaign_id）
    timeout_simple: int = 300  # 简单代码生成（秒）
    timeout_complex: int = 600  # 复杂调试（秒）
    timeout_module: int = 1200  # 完整模块实现（秒）
    fallback_to_agentloop: bool = True  # CC 失败时降级到 AgentLoop


@dataclass(frozen=True)
class HarnessConfig:
    """自建 Harness 配置（大修计划书第一章；第六章特性开关登记处）。

    ``features`` 是全大修的特性开关表（Ch6 §4.3）：每个新机制一个开关、
    默认关闭、逐里程碑打开——出问题关开关即回退，不需要回滚代码。
    """

    tool_menu: str = "config/tool_menu.yaml"  # 声明式工具菜单（缺文件回退代码声明）
    write_gate_enabled: bool = True  # 先读后写闸门（M1 强化完成，默认开；可关回退）
    ssh_servers: dict = field(default_factory=dict)  # SSH 服务器档案（凭证只进配置）
    features: tuple[tuple[str, bool], ...] = ()  # 特性开关（有序对，读代码侧转 dict）

    def feature(self, name: str, default: bool = False) -> bool:
        return dict(self.features).get(name, default)


@dataclass(frozen=True)
class Config:
    """Top-level config object handed to the pipeline / CLI / server."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    acp: ACPConfig = field(default_factory=ACPConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT


# --- loading ----------------------------------------------------------------

def _coerce(data: dict) -> Config:
    """Best-effort coercion of a raw YAML mapping into a Config.

    Reads the nested sections (``llm``, ``timeouts``, …) directly, matching the
    YAML structure. Unknown keys are ignored (forward-compat); missing keys fall
    back to the dataclass defaults. We deliberately do NOT hard-fail on a stray
    key — the YAML is a human-edited file and a typo in one section shouldn't
    brick the whole loader (the strict schemas live in the pydantic models).
    """
    llm_raw = data.get("llm", {}) or {}
    llm = LLMConfig(
        model=str(llm_raw.get("model", LLMConfig.model)),
        max_retries=int(llm_raw.get("max_retries", LLMConfig.max_retries)),
        api_base=str(llm_raw.get("api_base", LLMConfig.api_base)),
        api_key_env=str(llm_raw.get("api_key_env", LLMConfig.api_key_env)),
        sub_agent_model=str(llm_raw.get("sub_agent_model", LLMConfig.sub_agent_model)),
        provider_options=dict(llm_raw.get("provider_options", {}) or {}),
        pricing=dict(llm_raw.get("pricing", {}) or {}),
    )

    timeouts_raw = data.get("timeouts", {}) or {}
    timeouts = TimeoutsConfig(
        llm=int(timeouts_raw.get("llm", TimeoutsConfig.llm)),
        llm_wall=int(timeouts_raw.get("llm_wall", TimeoutsConfig.llm_wall)),
        stage=int(timeouts_raw.get("stage", TimeoutsConfig.stage)),
    )

    budget_raw = data.get("budget", {}) or {}
    budget = BudgetConfig(
        per_campaign=float(budget_raw.get("per_campaign", BudgetConfig.per_campaign)),
        global_limit=float(budget_raw.get("global_limit", BudgetConfig.global_limit)),
    )

    pipeline_raw = data.get("pipeline", {}) or {}
    pipeline = PipelineConfig(
        max_design_rounds=int(
            pipeline_raw.get("max_design_rounds", PipelineConfig.max_design_rounds)
        ),
        max_review_rounds=int(
            pipeline_raw.get("max_review_rounds", PipelineConfig.max_review_rounds)
        ),
        review_accept_threshold=float(
            pipeline_raw.get(
                "review_accept_threshold", PipelineConfig.review_accept_threshold
            )
        ),
        seek_candidate_count=int(
            pipeline_raw.get("seek_candidate_count", PipelineConfig.seek_candidate_count)
        ),
        max_exp_rounds=int(
            pipeline_raw.get("max_exp_rounds", PipelineConfig.max_exp_rounds)
        ),
        max_exp_to_design_rounds=int(
            pipeline_raw.get("max_exp_to_design_rounds", PipelineConfig.max_exp_to_design_rounds)
        ),
        output_candidate_count=int(
            pipeline_raw.get("output_candidate_count", PipelineConfig.output_candidate_count)
        ),
        stage_tool_limits={
            str(k).upper(): max(0, int(v))
            for k, v in (pipeline_raw.get("stage_tool_limits") or {}).items()
        },
    )

    server_raw = data.get("server", {}) or {}
    server = ServerConfig(
        host=str(server_raw.get("host", ServerConfig.host)),
        port=int(server_raw.get("port", ServerConfig.port)),
    )

    storage_raw = data.get("storage", {}) or {}
    storage = StorageConfig(
        db_path=str(storage_raw.get("db_path", StorageConfig.db_path)),
        campaigns_dir=str(storage_raw.get("campaigns_dir", StorageConfig.campaigns_dir)),
    )

    tools = _coerce_tools(data.get("tools", {}) or {})
    logging = _coerce_logging(data.get("logging", {}) or {})
    ssh = _coerce_ssh(data.get("ssh", {}) or {})
    vision = _coerce_vision(data.get("vision", {}) or {})
    acp = _coerce_acp(data.get("acp", {}) or {})
    memory_raw = data.get("memory", {}) or {}
    memory = MemoryConfig(
        enabled=bool(memory_raw.get("enabled", MemoryConfig.enabled)),
        memory_dir=str(memory_raw.get("memory_dir", MemoryConfig.memory_dir)),
        injection_max_items=int(
            memory_raw.get("injection_max_items", MemoryConfig.injection_max_items)
        ),
        inject_into_novelty=bool(
            memory_raw.get("inject_into_novelty", MemoryConfig.inject_into_novelty)
        ),
    )

    harness_raw = data.get("harness", {}) or {}
    harness = HarnessConfig(
        tool_menu=str(harness_raw.get("tool_menu", HarnessConfig.tool_menu)),
        write_gate_enabled=bool(
            harness_raw.get("write_gate_enabled", HarnessConfig.write_gate_enabled)
        ),
        ssh_servers={
            str(k): dict(v) for k, v in (harness_raw.get("ssh_servers") or {}).items()
            if isinstance(v, dict)
        },
        features=tuple(
            (str(k), bool(v))
            for k, v in (harness_raw.get("features") or {}).items()
        ),
    )

    return Config(
        llm=llm,
        timeouts=timeouts,
        budget=budget,
        pipeline=pipeline,
        server=server,
        storage=storage,
        tools=tools,
        logging=logging,
        ssh=ssh,
        vision=vision,
        acp=acp,
        memory=memory,
        harness=harness,
    )


def _coerce_vision(raw: dict) -> VisionConfig:
    """Build :class:`VisionConfig` from the raw ``vision:`` YAML mapping."""
    raw = raw or {}
    return VisionConfig(
        provider=str(raw.get("provider", VisionConfig.provider)),
        model=str(raw.get("model", VisionConfig.model)),
        api_base=str(raw.get("api_base", VisionConfig.api_base)),
        api_key_env=str(raw.get("api_key_env", VisionConfig.api_key_env)),
        max_tokens=int(raw.get("max_tokens", VisionConfig.max_tokens)),
    )


def _coerce_acp(raw: dict) -> ACPConfig:
    """Build :class:`ACPConfig` from the raw ``acp:`` YAML mapping."""
    raw = raw or {}
    return ACPConfig(
        enabled=bool(raw.get("enabled", ACPConfig.enabled)),
        acpx_path=str(raw.get("acpx_path", ACPConfig.acpx_path)),
        session_prefix=str(raw.get("session_prefix", ACPConfig.session_prefix)),
        timeout_simple=int(raw.get("timeout_simple", ACPConfig.timeout_simple)),
        timeout_complex=int(raw.get("timeout_complex", ACPConfig.timeout_complex)),
        timeout_module=int(raw.get("timeout_module", ACPConfig.timeout_module)),
        fallback_to_agentloop=bool(raw.get("fallback_to_agentloop", ACPConfig.fallback_to_agentloop)),
    )


def _coerce_ssh(raw: dict) -> SSHConfig:
    """Build :class:`SSHConfig` from the raw ``ssh:`` YAML mapping."""
    raw = raw or {}
    return SSHConfig(
        host=str(raw.get("host", SSHConfig.host)),
        user=str(raw.get("user", SSHConfig.user)),
        key_path=str(raw.get("key_path", SSHConfig.key_path)),
        remote_base_dir=str(raw.get("remote_base_dir", SSHConfig.remote_base_dir)),
        poll_interval_s=int(raw.get("poll_interval_s", SSHConfig.poll_interval_s)),
        poll_timeout_s=int(raw.get("poll_timeout_s", SSHConfig.poll_timeout_s)),
        control_path=str(raw.get("control_path", SSHConfig.control_path)),
    )


def _coerce_logging(raw: dict) -> LoggingConfig:
    """Build a :class:`LoggingConfig` from the raw ``logging:`` YAML mapping."""
    raw = raw or {}
    return LoggingConfig(
        level=str(raw.get("level", LoggingConfig.level)).upper(),
        format=str(raw.get("format", LoggingConfig.format)).lower(),
        output=str(raw.get("output", LoggingConfig.output)).lower(),
        file_path=str(raw.get("file_path", LoggingConfig.file_path)),
    )


def _as_tuple(value: object) -> tuple[str, ...]:
    """YAML lists → tuple (frozen dataclasses need immutable defaults)."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _coerce_tools(raw: dict) -> ToolsConfig:
    """Build a :class:`ToolsConfig` from the raw ``tools:`` YAML mapping."""
    ws_raw = raw.get("web_search", {}) or {}
    wf_raw = raw.get("web_fetch", {}) or {}
    eb_raw = raw.get("exec_bash", {}) or {}
    fo_raw = raw.get("file_ops", {}) or {}
    sp_raw = raw.get("search_paper", {}) or {}

    web_search = WebSearchConfig(
        provider=str(ws_raw.get("provider", WebSearchConfig.provider)),
        api_key_env=str(ws_raw.get("api_key_env", WebSearchConfig.api_key_env)),
        max_results=int(ws_raw.get("max_results", WebSearchConfig.max_results)),
    )
    web_fetch = WebFetchConfig(
        timeout=int(wf_raw.get("timeout", WebFetchConfig.timeout)),
        max_chars=int(wf_raw.get("max_chars", WebFetchConfig.max_chars)),
    )
    exec_bash = ExecBashConfig(
        timeout=int(eb_raw.get("timeout", ExecBashConfig.timeout)),
        blocked_commands=_as_tuple(
            eb_raw.get("blocked_commands", ExecBashConfig.blocked_commands)
        )
        or ExecBashConfig.blocked_commands,
    )
    file_ops = FileOpsConfig(
        allowed_paths=_as_tuple(
            fo_raw.get("allowed_paths", FileOpsConfig.allowed_paths)
        )
        or FileOpsConfig.allowed_paths,
        max_file_size_mb=int(fo_raw.get("max_file_size_mb", FileOpsConfig.max_file_size_mb)),
    )
    search_paper = SearchPaperConfig(
        provider=str(sp_raw.get("provider", SearchPaperConfig.provider)),
        api_key_env=str(sp_raw.get("api_key_env", SearchPaperConfig.api_key_env)),
        max_results=int(sp_raw.get("max_results", SearchPaperConfig.max_results)),
        fallback_chain=_as_tuple(
            sp_raw.get("fallback_chain", SearchPaperConfig.fallback_chain)
        ) or SearchPaperConfig.fallback_chain,
        email=str(sp_raw.get("email", SearchPaperConfig.email)),
    )
    ft_raw = raw.get("fulltext", {}) or {}
    ll_raw = ft_raw.get("local_library", {}) or {}
    fulltext = FulltextConfig(
        email=str(ft_raw.get("email", FulltextConfig.email)),
        timeout_s=int(ft_raw.get("timeout_s", FulltextConfig.timeout_s)),
        max_mb=int(ft_raw.get("max_mb", FulltextConfig.max_mb)),
        retries=int(ft_raw.get("retries", FulltextConfig.retries)),
        local_library=LocalLibraryConfig(
            enabled=bool(ll_raw.get("enabled", LocalLibraryConfig.enabled)),
            dir=str(ll_raw.get("dir", LocalLibraryConfig.dir)),
        ),
    )
    return ToolsConfig(
        web_search=web_search,
        web_fetch=web_fetch,
        exec_bash=exec_bash,
        file_ops=file_ops,
        search_paper=search_paper,
        agent_result_max_chars=int(
            raw.get("agent_result_max_chars", ToolsConfig.agent_result_max_chars)
        ),
        read_file_result_max_chars=int(
            raw.get("read_file_result_max_chars", ToolsConfig.read_file_result_max_chars)
        ),
        fulltext=fulltext,
    )


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Decide which YAML file to load (explicit > HAA_CONFIG > default)."""
    if path is not None:
        return Path(path)
    env = os.environ.get("HAA_CONFIG")
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the YAML config into a :class:`Config`.

    If the file is missing, returns the built-in defaults (so the CLI and tests
    work without the file present) rather than crashing.
    """
    p = resolve_config_path(path)
    try:
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return Config()
    if not isinstance(raw, dict):
        raise ValueError(f"config file {p} must contain a YAML mapping at the top level")
    return _coerce(raw)


def default_config() -> Config:
    """Convenience: load config/default.yaml (the bundled defaults)."""
    return load_config(DEFAULT_CONFIG_PATH)
