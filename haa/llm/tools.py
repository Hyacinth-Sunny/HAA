"""Tool-calling layer (Phase 2, "方案二"): a self-built function-calling sandbox.

HAA does **not** hand execution to an external agent system. It registers small
Python handlers behind OpenAI-style JSON schemas, lets the model call them via
litellm's ``tools`` parameter, and executes them itself (see
:mod:`haa.llm.agent_loop`). This keeps HAA's core philosophy intact: *the code
decides what runs where* — each tool carries an ``allowed_stages`` set and the
registry filters by the active stage.

Six tools, modelled on Claude Code's core set (Read/Write/Bash/WebSearch/WebFetch):

================== ============================== =================================
tool               Claude Code analogue           allowed stages
================== ============================== =================================
``web_search``     WebSearch                       SEEK, NOVELTY
``web_fetch``      WebFetch                        SEEK, NOVELTY, SCREEN
``read_file``      Read                            all stages (``ALL_STAGES`` sentinel)
``write_file``     Write                           WRITE, REFINE
``exec_bash``      Bash                            SCREEN, DESIGN, VERIFY
``search_paper``   (no direct analogue)            SEEK, NOVELTY, SCREEN
================== ============================== =================================

Safety
------
* **Path sandbox** (:func:`_safe_resolve`): file tools resolve relative to the
  campaign dir and must land under it (or an allow-listed root). ``resolve()``
  follows symlinks, so a symlink that escapes the sandbox is rejected.
* **Command filter + timeout** (HM-Pro Lesson 7): ``exec_bash`` blocks dangerous
  patterns and is killed after ``exec_bash.timeout`` seconds.
* A blocked command or an escaped path raises :class:`ToolSafetyError` rather
  than silently running.

Testability
-----------
The two HTTP helpers (:func:`_http_get_json`, :func:`_http_get_text`) are
module-level so tests monkeypatch them; no network is needed for the suite.

Config lives in :class:`haa.config.ToolsConfig`; file resolution reuses
``haa.config.resolve_config_path`` so ``HAA_CONFIG`` overrides still work.
"""

from __future__ import annotations

import html as _html_mod
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from haa.config import ToolsConfig

logger = logging.getLogger("haa.tools")

# Sentinel stage value meaning "allowed in every stage".
ALL_STAGES = "*"


# ---------------------------------------------------------------------------
# Errors + data types
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A tool failed at runtime (bad args, timeout, missing file, …)."""


class ToolPermissionError(ToolError):
    """A tool was invoked from a stage it is not allowed in."""


class ToolSafetyError(ToolError):
    """A tool call violated a safety rule (path escape / blocked command)."""


@dataclass
class ToolCallContext:
    """Per-invocation context handed to a handler."""

    stage_name: str = ""
    campaign_id: str = ""
    campaigns_dir: Path = field(default_factory=lambda: Path("data/campaigns"))
    allowed_roots: tuple[Path, ...] = ()
    config: ToolsConfig = field(default_factory=ToolsConfig)
    # The LLM client, injected by the pipeline so multi-agent tools can spawn
    # sub-agent calls. None in tests / ad-hoc use (multi_agents then raises).
    client: Any = None
    # 生效配置的 vision 段（随 HAA_CONFIG 走；此前 describe_image 等工具硬编码
    # default_config().vision，glm-flash.yaml 的 vision 配置对工具不生效——
    # v1.0.6-rev2 修复）。None 时回退 default。
    vision_config: Any = None


@dataclass
class ToolDefinition:
    """One tool: schema for the model + handler for execution + stage permissions."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (Lesson 6: required must be complete)
    allowed_stages: frozenset[str]
    handler: Callable[[dict, ToolCallContext], str]

    def is_allowed_for(self, stage_name: str) -> bool:
        if not stage_name:
            return True  # no stage context → do not block (tests / ad-hoc use)
        return stage_name in self.allowed_stages or ALL_STAGES in self.allowed_stages

    def to_openai_schema(self) -> dict[str, Any]:
        """OpenAI function-calling tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Holds tool definitions and enforces per-stage permissions on execution."""

    def __init__(
        self,
        tools_config: ToolsConfig | None = None,
        *,
        campaigns_dir: str | Path = "data/campaigns",
        allowed_roots: list[Path] | None = None,
        tools: list[ToolDefinition] | None = None,
        client: Any = None,
        vision_config: Any = None,
    ) -> None:
        self.config = tools_config or ToolsConfig()
        self.campaigns_dir = Path(campaigns_dir)
        self.allowed_roots = tuple(allowed_roots or ())
        self.client = client  # for multi_agents (sub-agent calls)
        self.vision_config = vision_config
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools if tools is not None else _default_tool_definitions():
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_allowed(self, name: str, stage_name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.is_allowed_for(stage_name)

    def get_schemas(self, stage_name: str) -> list[dict[str, Any]]:
        """OpenAI schemas for the tools the given stage may use."""
        return [
            t.to_openai_schema()
            for t in self._tools.values()
            if t.is_allowed_for(stage_name)
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        stage_name: str = "",
        campaign_id: str = "",
    ) -> str:
        """Run a tool after a stage-permission check. Always returns a string.

        Safety/permission violations raise; handler runtime errors raise
        :class:`ToolError` (the agent loop catches these and feeds them back).
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name!r}")
        if stage_name and not tool.is_allowed_for(stage_name):
            raise ToolPermissionError(
                f"tool {name!r} is not allowed in stage {stage_name!r}"
            )
        args = arguments if isinstance(arguments, dict) else {}
        ctx = ToolCallContext(
            stage_name=stage_name,
            campaign_id=campaign_id,
            campaigns_dir=self.campaigns_dir,
            allowed_roots=self.allowed_roots,
            config=self.config,
            client=self.client,
            vision_config=self.vision_config,
        )
        return tool.handler(args, ctx)


# ===========================================================================
# Security helpers
# ===========================================================================


def _safe_resolve(
    path_str: str,
    campaign_id: str,
    campaigns_dir: Path,
    allowed_roots: tuple[Path, ...],
) -> Path:
    """Resolve ``path_str`` and verify it lands inside an allowed root.

    Relative paths resolve against the per-campaign working dir
    (``campaigns_dir / campaign_id``). ``Path.resolve()`` follows symlinks, so a
    symlink that points outside the sandbox is caught by the ``relative_to``
    check and rejected.
    """
    base = Path(campaigns_dir)
    if campaign_id:
        base = base / campaign_id
    base = base.resolve()

    p = Path(path_str)
    if not p.is_absolute():
        p = base / p
    p = p.resolve()

    for root in (base, *allowed_roots):
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise ToolSafetyError(f"path escapes the sandbox: {path_str!r}")


def _check_command(command: str, blocked: tuple[str, ...]) -> None:
    """Reject commands matching any blocked substring pattern."""
    low = command.lower()
    for pat in blocked:
        if pat.lower() in low:
            raise ToolSafetyError(f"blocked command pattern: {pat.strip()!r}")


# ===========================================================================
# HTTP helpers (module-level → monkeypatchable in tests)
# ===========================================================================


def _http_get_json(
    url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = 30
) -> dict:
    import httpx

    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def _http_get_text(
    url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = 30
) -> str:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.text


def _http_post_json(
    url: str, *, json_body: dict | None = None, headers: dict | None = None, timeout: int = 30
) -> dict:
    """POST JSON and return parsed JSON (Tavily requires POST + body, not GET)."""
    import httpx

    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=json_body or {}, headers=headers)
        r.raise_for_status()
        return r.json()


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Crude HTML → text: drop script/style, strip tags, collapse whitespace."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _html_mod.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _validate_url(url: str) -> str:
    """Outbound-URL safety gate (v1.0.6-rev2, Mimosa 约束对齐):

    仅放行 http/https；拒绝 localhost/环回/私有/保留地址与内网域名后缀。
    HAA 长期跑外网检索，模型可投喂任意 URL——此闸防 SSRF 探内网。
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolSafetyError(
            f"URL rejected: scheme {parsed.scheme!r} not allowed (http/https only): {url!r}"
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ToolSafetyError(f"URL rejected: no hostname: {url!r}")
    if host in ("localhost",) or host.endswith(".localhost") \
            or host.endswith(".local") or host.endswith(".internal"):
        raise ToolSafetyError(f"URL rejected: internal hostname {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url  # 普通域名：DNS 解析处的防护属更深层，此处不展开
    if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local \
            or ip.is_multicast or ip.is_unspecified:
        raise ToolSafetyError(
            f"URL rejected: non-public address {host!r} (private/loopback/reserved)"
        )
    return url


# ===========================================================================
# Tool handlers
# ===========================================================================


def _web_search(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.web_search
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ToolError("web_search: 'query' is required")
    max_results = int(arguments.get("max_results") or cfg.max_results)
    api_key = os.environ.get(cfg.api_key_env, "")
    if not api_key:
        return f"web_search unavailable: env var {cfg.api_key_env} is not set"

    if cfg.provider != "tavily":
        return f"web_search: provider {cfg.provider!r} not implemented"

    # Tavily requires POST + JSON body (GET with api_key query param → 401).
    data = _http_post_json(
        "https://api.tavily.com/search",
        json_body={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=30,
    )
    results = data.get("results", []) if isinstance(data, dict) else []
    lines = [
        f"- {r.get('title', '(no title)')}: {r.get('url', '')}\n"
        f"  {_truncate(r.get('content', ''), 300)}"
        for r in results[:max_results]
        if isinstance(r, dict)
    ]
    return "\n".join(lines) if lines else f"no results for {query!r}"


def _web_fetch(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.web_fetch
    url = str(arguments.get("url", "")).strip()
    if not url:
        raise ToolError("web_fetch: 'url' is required")
    max_chars = int(arguments.get("max_chars") or cfg.max_chars)
    _validate_url(url)
    raw = _http_get_text(url, timeout=cfg.timeout)
    text = _strip_html(raw) if "<" in raw else raw
    return _truncate(text, max_chars)


def _list_campaign_files(ctx: ToolCallContext, per_dir: int = 20) -> str:
    """List files that actually exist in the campaign dir (top level +
    artifacts/), for grounding read_file's not-found error — kills the
    path-hallucination loop by showing the model what IS on disk."""
    try:
        root = Path(ctx.campaigns_dir) / ctx.campaign_id
        names: list[str] = []
        if root.is_dir():
            names.extend(
                sorted(p.name for p in root.iterdir() if p.is_file())[:per_dir]
            )
        art = root / "artifacts"
        if art.is_dir():
            art_names = sorted(p.name for p in art.iterdir() if p.is_file())[:per_dir]
            names.extend(f"artifacts/{n}" for n in art_names)
            for sub in sorted(p for p in art.iterdir() if p.is_dir()):
                if sub.name.startswith("_") or len(names) > per_dir * 2:
                    continue
                sub_names = sorted(q.name for q in sub.iterdir() if q.is_file())[:6]
                names.extend(f"artifacts/{sub.name}/{n}" for n in sub_names)
        return ", ".join(names) if names else "(directory is empty)"
    except Exception:
        return "(unavailable)"


def _read_file(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.file_ops
    path_str = str(arguments.get("path", "")).strip()
    if not path_str:
        raise ToolError("read_file: 'path' is required")
    max_chars = int(arguments.get("max_chars") or 50000)
    p = _safe_resolve(path_str, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
    if not p.exists() or not p.is_file():
        raise ToolError(
            f"not a readable file: {path_str!r}. "
            f"Files that exist here: {_list_campaign_files(ctx)}. "
            f"(Stage outputs live in the conversation context; "
            f"only the files listed above exist on disk.)"
        )
    size = p.stat().st_size
    if size > cfg.max_file_size_mb * 1024 * 1024:
        raise ToolSafetyError(f"file too large ({size} bytes > {cfg.max_file_size_mb}MB)")
    return _truncate(p.read_text(encoding="utf-8", errors="replace"), max_chars)


def _write_file(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.file_ops
    path_str = str(arguments.get("path", "")).strip()
    content = str(arguments.get("content", ""))
    if not path_str:
        raise ToolError("write_file: 'path' is required")
    if len(content.encode("utf-8")) > cfg.max_file_size_mb * 1024 * 1024:
        raise ToolSafetyError(f"content exceeds {cfg.max_file_size_mb}MB")
    p = _safe_resolve(path_str, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path_str!r}"


def _exec_bash(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.exec_bash
    command = str(arguments.get("command", "")).strip()
    if not command:
        raise ToolError("exec_bash: 'command' is required")
    # 模型可传 timeout 但有硬顶（smoke4 复盘：原实现可传 99999 无上限）。
    timeout = _clamp_exec_timeout(arguments.get("timeout"), cfg.timeout)
    _check_command(command, cfg.blocked_commands)

    cwd = Path(ctx.campaigns_dir)
    if ctx.campaign_id:
        cwd = cwd / ctx.campaign_id
    cwd.mkdir(parents=True, exist_ok=True)

    # start_new_session=True + 超时 killpg：杀整个进程组——shell 的后台
    # 孙进程（"sleep 300 &"）不存活；subprocess.run 只杀直接子进程。
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise ToolError(f"exec_bash timed out after {timeout}s") from exc

    output = (stdout or "") + (stderr or "")
    return _truncate(output.strip(), 50000) or "(no output)"


_EXEC_BASH_MAX_TIMEOUT_S = 120


def _clamp_exec_timeout(raw: Any, default: int) -> int:
    """Model-supplied timeout clamped to [1, hard ceiling]; default from config."""
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(1, min(value, _EXEC_BASH_MAX_TIMEOUT_S))


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group of ``proc`` (best-effort)."""
    import signal as _signal

    try:
        os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()  # 组已消失或不可杀——退回杀直接子进程
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # noqa: B012 — 不可等待也不阻塞主流程
        pass


def _search_paper(arguments: dict, ctx: ToolCallContext) -> str:
    cfg = ctx.config.search_paper
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ToolError("search_paper: 'query' is required")
    max_results = int(arguments.get("max_results") or cfg.max_results)

    headers: dict | None = None
    if cfg.api_key_env:
        key = os.environ.get(cfg.api_key_env, "")
        if key:
            headers = {"x-api-key": key}

    try:
        data = _http_get_json(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": query,
                "limit": max_results,
                "fields": "title,abstract,year,citationCount,externalIds,openAccessPdf",
            },
            headers=headers,
            timeout=30,
        )
    except Exception:
        data = None
    papers = data.get("data", []) if isinstance(data, dict) else []
    if not papers:
        # v1.0 回退链：S2 失败/空 → OpenAlex → arXiv（冒烟审计：S2 无 key
        # 共享限流池在本机 100% 失败；OpenAlex 无需 key 且带引用数）。
        for fallback in getattr(cfg, "fallback_chain", ()) or ():
            try:
                if fallback == "openalex":
                    lines = _search_openalex(query, max_results, cfg)
                elif fallback == "arxiv":
                    lines = _search_arxiv(query, max_results)
                else:
                    continue
                if lines:
                    return lines
            except Exception:
                continue
        return f"no papers found for {query!r} (all sources failed)"

    lines = []
    for paper in papers[:max_results]:
        if not isinstance(paper, dict):
            continue
        ext = paper.get("externalIds") or {}
        oa = paper.get("openAccessPdf") or {}
        pdf = f" PDF={oa.get('url', '')}" if isinstance(oa, dict) and oa.get("url") else ""
        lines.append(
            f"- {paper.get('title', '(no title)')} "
            f"({paper.get('year', '?')}, cites={paper.get('citationCount', 0)}) "
            f"[DOI={ext.get('DOI', '')}]{pdf}\n"
            f"  {_truncate(paper.get('abstract') or '', 300)}"
        )
    return "\n".join(lines) if lines else f"no papers found for {query!r}"


def _search_openalex(query: str, max_results: int, cfg: Any) -> str:
    """OpenAlex fallback — no API key required; includes citation counts.

    A ``mailto`` in config enters the polite pool (faster, more stable).
    """
    params: dict[str, Any] = {"search": query, "per-page": min(max_results, 25)}
    email = str(getattr(cfg, "email", "") or "").strip()
    if email:
        params["mailto"] = email
    data = _http_get_json(
        "https://api.openalex.org/works",
        params=params,
        timeout=30,
    )
    works = data.get("results", []) if isinstance(data, dict) else []
    lines = []
    for w in works[:max_results]:
        if not isinstance(w, dict):
            continue
        title = w.get("display_name") or "(no title)"
        year = w.get("publication_year") or "?"
        cites = w.get("cited_by_count") or 0
        url = w.get("id") or ""
        doi = str(w.get("doi") or "").replace("https://doi.org/", "")
        abstract = _openalex_abstract(w)
        oa = w.get("best_oa_location") or {}
        pdf = f" PDF={oa.get('pdf_url', '')}" if isinstance(oa, dict) and oa.get("pdf_url") else ""
        lines.append(
            f"- {title} ({year}, cites={cites}) [DOI={doi}]{pdf} {url}\n"
            f"  {_truncate(abstract, 300)}"
        )
    return "\n".join(lines)


def _openalex_abstract(work: dict) -> str:
    """OpenAlex stores abstracts as inverted-index; rebuild the text."""
    inv = work.get("abstract_inverted_index")
    if not isinstance(inv, dict) or not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _search_arxiv(query: str, max_results: int) -> str:
    """arXiv Atom API fallback — preprint coverage, no key needed."""
    import urllib.parse
    import urllib.request
    import xml.etree.ElementTree as ET

    url = (
        "http://export.arxiv.org/api/query?search_query=all:"
        + urllib.parse.quote(query)
        + f"&max_results={min(max_results, 20)}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    lines = []
    for entry in root.findall("a:entry", ns)[:max_results]:
        title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
        published = (entry.findtext("a:published", "", ns) or "")[:4]
        summary = (entry.findtext("a:summary", "", ns) or "").strip()
        link = entry.findtext("a:id", "", ns) or ""
        pdf = link.replace("/abs/", "/pdf/") if "/abs/" in link else ""
        lines.append(
            f"- {title} ({published or '?'}, preprint)"
            f"{' PDF=' + pdf if pdf else ''} {link}\n  {_truncate(summary, 300)}"
        )
    return "\n".join(lines)


# ===========================================================================
# Phase v0.1 tools: edit_file / to_do_write / multi_agents / calculator
# ===========================================================================


def _edit_file(arguments: dict, ctx: ToolCallContext) -> str:
    """Precise in-place string replacement (vs write_file's full overwrite)."""
    cfg = ctx.config.file_ops
    path_str = str(arguments.get("path", "")).strip()
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    replace_all = bool(arguments.get("replace_all", False))
    if not path_str or old is None or new is None:
        raise ToolError("edit_file: 'path', 'old_string', 'new_string' are required")
    if old == new:
        raise ToolError("edit_file: old_string == new_string (nothing to change)")
    p = _safe_resolve(path_str, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
    if not p.is_file():
        raise ToolError(f"not an existing file: {path_str!r}")
    text = p.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old)
    if occurrences == 0:
        raise ToolError(f"old_string not found in {path_str!r}")
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"old_string matches {occurrences} times in {path_str!r}; "
            "narrow it to a unique fragment or set replace_all=true"
        )
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    if len(new_text.encode("utf-8")) > cfg.max_file_size_mb * 1024 * 1024:
        raise ToolSafetyError(f"result would exceed {cfg.max_file_size_mb}MB")
    p.write_text(new_text, encoding="utf-8")
    n = occurrences if replace_all else 1
    return f"replaced {n} occurrence(s) in {path_str!r}"


def _to_do_write(arguments: dict, ctx: ToolCallContext) -> str:
    """Persist a step-by-step task list for the current stage → campaign dir."""
    raw_todos = arguments.get("todos", []) or []
    if not isinstance(raw_todos, list) or not raw_todos:
        raise ToolError("to_do_write: 'todos' must be a non-empty list")
    norm = []
    for item in raw_todos[:50]:
        if isinstance(item, str):
            norm.append({"content": item, "status": "pending", "active_form": item})
        elif isinstance(item, dict):
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            status = str(item.get("status", "pending")).strip().lower()
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            norm.append({
                "content": content,
                "status": status,
                "active_form": str(item.get("active_form", content)),
            })
    if not norm:
        raise ToolError("to_do_write: no valid todo items after parsing")
    base = Path(ctx.campaigns_dir) / (ctx.campaign_id or "_adhoc")
    base.mkdir(parents=True, exist_ok=True)
    import json as _json
    path = base / "todo.json"
    path.write_text(_json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8")
    done = sum(1 for t in norm if t["status"] == "completed")
    return f"todo list saved ({len(norm)} items, {done} done) → {path.name}"


def _multi_agents(arguments: dict, ctx: ToolCallContext) -> str:
    """Fan out N independent sub-agents (one LLM call each), in parallel.

    For multi-angle work: SEEK (ideas from different sub-fields), SCREEN (attack
    one candidate from several angles), REVIEW-style panels. Each sub-agent is a
    plain LLM call with NO tools → never nests an agentic loop.
    """
    if ctx.client is None:
        raise ToolError("multi_agents: no LLM client available in this context")
    tasks = arguments.get("tasks", []) or []
    if not isinstance(tasks, list) or not tasks:
        raise ToolError("multi_agents: 'tasks' must be a non-empty list")
    tasks = tasks[:6]  # hard cap on parallel fan-out

    def _run_one(task):
        if isinstance(task, dict):
            role = str(task.get("role", "")) or "sub-agent"
            prompt = str(task.get("prompt", ""))
        else:
            role, prompt = "sub-agent", str(task)
        if not prompt.strip():
            return role, "(empty prompt)"
        try:
            resp = ctx.client.call(
                [{"role": "user", "content": prompt}],
                campaign_id=ctx.campaign_id or None,
                stage=ctx.stage_name or None,
            )
            return role, resp.content or "(no content)"
        except Exception as exc:  # one sub-agent failure must not sink the rest
            return role, f"(sub-agent failed: {type(exc).__name__}: {exc})"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as pool:
        results = list(pool.map(_run_one, tasks))
    # 子代理全文直接进对话会撑爆循环内上下文（smoke4 复盘：DESIGN 峰值
    # 51.7K in）——每个子代理结果截到 4K 字符。
    blocks = [f"### {role}\n{_truncate(body, 4000)}" for role, body in results]
    return "\n\n---\n\n".join(blocks)


# --- calculator helpers --------------------------------------------------

_SAFE_NUMERIC_BUILTINS = {
    "abs": abs, "min": min, "max": max, "sum": sum, "round": round,
    "pow": pow, "range": range, "len": len, "bool": bool, "int": int, "float": float,
}


def _identifiers(text: str) -> set[str]:
    import re
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or ""))


def _calc_numeric(expression: str) -> str:
    """Evaluate a numeric Python expression in a restricted namespace."""
    import math

    def _compute() -> str:
        result = eval(expression, {"__builtins__": {}}, {**_SAFE_NUMERIC_BUILTINS, "math": math})
        return repr(result)

    try:
        return _run_isolated(_compute, _CALC_TIMEOUT_S)
    except _CalcTimeout as exc:
        raise ToolError(
            f"numeric eval timed out after {_CALC_TIMEOUT_S:.0f}s — the expression "
            f"(likely huge bigint powers) is too expensive; reduce the exponents."
        ) from exc
    except _CalcChildError as exc:
        raise ToolError(f"numeric eval failed: {exc}") from exc


# sympy 病态表达式可无限燃烧 CPU（smoke4 实证：VERIFY 阶段一次 simplify
# 挂死主线程 25min+，全管线停摆）。sympy 纯 Python 无法从另一线程打断
# （SIGALRM 仅主线程有效，而 API 服务器在工作线程跑管线——第一版护栏在
# 服务器路径会静默失效），故改为 fork 进程隔离：超时 terminate()，任何
# 线程下行为一致。子进程只算术+写 Pipe，不碰 DB/网络。
_CALC_TIMEOUT_S = 30.0


class _CalcTimeout(Exception):
    """The isolated calc child exceeded its CPU budget."""


class _CalcChildError(Exception):
    """The isolated calc child raised (parse/eval error inside the variant)."""


def _run_isolated(compute: Any, seconds: float) -> str:
    """Run ``compute() -> str`` in a forked child bounded by ``seconds``.

    The child returns the final *string* (not the sympy object) — repr is
    computed child-side so nothing exotic needs to pickle across the Pipe.
    """
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)

    def _worker() -> None:
        try:
            child_conn.send(("ok", compute()))
        except BaseException as exc:  # noqa: BLE001 — 子进程内任何异常都回传
            try:
                child_conn.send(("err", f"{type(exc).__name__}: {exc}"))
            except Exception:  # noqa: BLE001
                pass
        finally:
            child_conn.close()

    proc = ctx.Process(target=_worker, daemon=True)
    proc.start()
    try:
        proc.join(seconds)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            raise _CalcTimeout(f"calc exceeded {seconds:.0f}s")
        if parent_conn.poll():
            status, payload = parent_conn.recv()
        else:
            status, payload = "err", f"calc child died without a word (exitcode={proc.exitcode})"
        if status == "err":
            raise _CalcChildError(payload)
        return payload
    finally:
        parent_conn.close()


def _calc_symbolic(expression: str) -> str:
    """Evaluate a SymPy expression. DSL: optional leading verb
    (simplify/expand/factor/diff/integrate/solve) then the expr; else sympify.

    Tolerant parsing (v0.9.1 — smoke audit: 35% of DESIGN-stage calls failed
    on prose/fenced input): try raw, then progressively cleaned variants
    (strip ``` fences, strip a ``sympy:`` prefix, take the last math-looking
    line). Success echoes the parsed expression so the model can confirm what
    the tool understood; failure names the format with an example.
    """
    import sympy as sp
    verbs = {
        "simplify": sp.simplify, "expand": sp.expand, "factor": sp.factor,
        "diff": sp.diff, "integrate": sp.integrate, "solve": sp.solve,
    }
    local = {n: sp.Symbol(n) for n in _identifiers(expression)}

    last_exc: Exception | None = None
    for candidate_expr in _symbolic_candidates(expression):
        try:
            # Verb 两种写法都收：空格分隔 "simplify x**2" 与函数调用
            # "simplify(x**2)"（后者是报错示例教给模型的格式，v0.9.1 前者
            # 独存导致示例本身解析失败——smoke4 复盘测试抓出）。
            call = re.fullmatch(
                r"(simplify|expand|factor|diff|integrate|solve)\s*\((.*)\)",
                candidate_expr.strip(),
                re.S,
            )
            parts = (
                [call.group(1), call.group(2)] if call
                else candidate_expr.strip().split(None, 1)
            )
            if len(parts) == 2 and parts[0] in verbs:
                out_repr = _run_isolated(
                    lambda: repr(verbs[parts[0]](sp.sympify(parts[1], locals=local))),
                    _CALC_TIMEOUT_S,
                )
                return f"parsed: {candidate_expr.strip()}\nresult: {out_repr}"
            out_repr = _run_isolated(
                lambda: repr(sp.sympify(candidate_expr, locals=local)), _CALC_TIMEOUT_S
            )
            return f"parsed: {candidate_expr.strip()}\nresult: {out_repr}"
        except _CalcTimeout as exc:
            raise ToolError(
                f"symbolic eval timed out after {_CALC_TIMEOUT_S:.0f}s — the "
                f"expression is too pathological to evaluate whole. Split it "
                f"into smaller sub-expressions, or reason about bounds "
                f"instead of exact symbolic forms."
            ) from exc
        except _CalcChildError as exc:  # 子进程内的解析失败 → 试下一个更激进清洗的变体
            last_exc = exc
        except Exception as exc:  # try the next, more aggressively cleaned variant
            last_exc = exc
    raise ToolError(
        f"symbolic eval failed: {type(last_exc).__name__ if last_exc else 'SyntaxError'}. "
        f"Pass ONLY a valid SymPy expression, e.g. simplify(x**2 + 2*x) or x**2 + 2*x — "
        f"no prose, no explanation text, no markdown fences."
    ) from last_exc


def _symbolic_candidates(expression: str) -> list[str]:
    """Progressively cleaned variants of the raw input, most-raw first."""
    raw = (expression or "").strip()
    variants = [raw]
    # 1. Strip markdown fences.
    lines = [ln for ln in raw.splitlines() if ln.strip() not in ("```", "```latex", "```python", "```text")]
    if len(lines) != len(raw.splitlines()):
        variants.append("\n".join(lines).strip())
    # 2. Strip a leading "sympy:" / "expr:" style prefix.
    lowered = raw.lower()
    for prefix in ("sympy:", "expr:", "expression:", "equation:"):
        if lowered.startswith(prefix):
            variants.append(raw[len(prefix):].strip())
    # 3. Take the LAST line that looks like math (has an operator/digit and no spaces-only).
    math_lines = [
        ln.strip().strip("`").rstrip(",;")
        for ln in raw.splitlines()
        if any(ch in ln for ch in "=+-*/^()0123456789") and len(ln.strip()) <= 300
    ]
    if math_lines:
        variants.append(math_lines[-1])
    # Dedup, preserve order.
    seen: set[str] = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _calc_counterexample(expression: str, params: dict) -> str:
    """Scan a small parameter space; return points where `expression` is False.

    `expression`: a Python bool expression over variable names (the claim).
    `params`: maps each variable to [lo, hi, step] or a literal list of values.
    """
    import itertools
    import math
    axes: dict[str, list] = {}
    for var, spec in (params or {}).items():
        if isinstance(spec, list) and len(spec) == 3 and all(isinstance(v, (int, float)) for v in spec):
            lo, hi, step = spec
            vals, v = [], lo
            while v <= hi + 1e-9:
                vals.append(round(v, 10)); v += step
            axes[var] = vals[:2000]
        elif isinstance(spec, list):
            axes[var] = list(spec[:2000])
        else:
            raise ToolError(f"counterexample: bad range for {var!r}: {spec!r}")
    if not axes:
        raise ToolError("counterexample: no parameter ranges given")

    counterexamples = []
    checked = 0
    env_base = {"__builtins__": {}, "math": math, "abs": abs, "min": min, "max": max}
    for combo in itertools.product(*(axes[v] for v in axes)):
        checked += 1
        env = {**env_base, **dict(zip(axes, combo))}
        try:
            holds = bool(eval(expression, env, env))
        except Exception:
            counterexamples.append(dict(zip(axes, combo)))  # ill-defined point = signal
        else:
            if not holds:
                counterexamples.append(dict(zip(axes, combo)))
        if len(counterexamples) >= 10:
            break
    if counterexamples:
        sample = ", ".join(str(c) for c in counterexamples[:10])
        return f"COUNTEREXAMPLE FOUND ({len(counterexamples)} in {checked} tries): {sample}"
    return f"no counterexample found in {checked} tried combinations"


def _calculator(arguments: dict, ctx: ToolCallContext) -> str:
    """Multi-mode calculator: numeric / symbolic / counterexample search.

    Why it exists (HM-Pro lesson): LLMs doing arithmetic or algebra by
    autoregression is unreliable and the errors are invisible. Route every
    non-trivial computation here instead.
    """
    mode = str(arguments.get("mode", "")).strip().lower()
    expression = str(arguments.get("expression", "")).strip()
    if not mode:
        raise ToolError("calculator: 'mode' required (numeric|symbolic|search_counterexample)")
    if not expression:
        raise ToolError("calculator: 'expression' required")
    if mode == "numeric":
        return _calc_numeric(expression)
    if mode == "symbolic":
        return _calc_symbolic(expression)
    if mode in ("search_counterexample", "counterexample"):
        return _calc_counterexample(expression, arguments.get("params", {}) or {})
    raise ToolError(f"calculator: unknown mode {mode!r}")


# --- grep (v1.0.6-rev2: 按需检索，替代知识文件全量整读) -----------------------

_GREP_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "_rejected", "figures"}
_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024  # 跳过超大文件（多为数据/二进制）


def _grep(arguments: dict, ctx: ToolCallContext) -> str:
    """正则检索 campaign 目录（含 knowledge/ 工件）内的文本文件。

    SEEK 消化 17 个知识文件靠 read_file 全量整读是撞帽主因之一（v1.1 议程
    #4）；grep 给模型"先检索定位、再窄读"的两段式能力。
    """
    import fnmatch

    pattern = str(arguments.get("pattern", "")).strip()
    if not pattern:
        raise ToolError("grep: 'pattern' is required (a Python regex)")
    path_filter = str(arguments.get("path", "") or "").strip()  # 限定子目录/通配
    max_matches = max(1, min(int(arguments.get("max_matches") or 50), 200))
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"grep: invalid regex: {exc}") from exc

    base = Path(ctx.campaigns_dir)
    if ctx.campaign_id:
        base = base / ctx.campaign_id
    if path_filter:
        base = _safe_resolve(path_filter, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
    base = base.resolve()
    # 沙箱根：campaign 目录本身或 allow-listed root
    roots = [base] + [
        r for r in [Path(ctx.campaigns_dir).resolve(), *ctx.allowed_roots]
    ]

    def _in_sandbox(p: Path) -> bool:
        return any(
            (p == r or r in p.parents) for r in roots
        )

    matches: list[str] = []
    files_scanned = 0
    if base.is_file():
        candidates = [base]
    else:
        candidates = []
        for p in base.rglob("*"):
            if any(part in _GREP_SKIP_DIRS for part in p.parts):
                continue
            if p.is_file():
                candidates.append(p)
    for p in sorted(candidates):
        try:
            if p.stat().st_size > _GREP_MAX_FILE_BYTES:
                continue
            files_scanned += 1
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(p.relative_to(base)) if _in_sandbox(p) else str(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                snippet = line.strip()
                if len(snippet) > 300:
                    snippet = snippet[:300] + "…"
                matches.append(f"{rel}:{lineno}: {snippet}")
                if len(matches) >= max_matches:
                    return (
                        f"{len(matches)} match(es) in {files_scanned} file(s)"
                        f" (capped at {max_matches}):\n" + "\n".join(matches)
                    )
    if not matches:
        return f"no matches for /{pattern}/ in {files_scanned} file(s) under {base}"
    return f"{len(matches)} match(es) in {files_scanned} file(s):\n" + "\n".join(matches)


# ===========================================================================
# Phase C tools: read_pdf / describe_image / visualization / illustration
# ===========================================================================


def _read_pdf(arguments: dict, ctx: ToolCallContext) -> str:
    """从本地路径或 URL 读取 PDF 并提取文本（URL 走鲁棒下载器，带缓存）。"""
    from haa.llm.pdf_utils import extract_pdf_text

    source = str(arguments.get("source", "")).strip()
    if not source:
        raise ToolError("read_pdf: 'source' is required (URL or local path)")
    max_chars = int(arguments.get("max_chars") or 50000)
    if source.startswith(("http://", "https://")):
        _validate_url(source)
        # v1.0.6-rev3：URL 分支改走 fulltext.fetch_pdf——重试+魔数校验+缓存
        # （旧 urllib 单次 30s 超时在 NAT 网络下失败成串，成功案例可到 138s）。
        from haa.llm.fulltext import FulltextError, fetch_pdf

        ft = getattr(ctx.config, "fulltext", None)
        cache_dir = Path(ctx.campaigns_dir) / (ctx.campaign_id or "_adhoc") / "knowledge" / "fulltext"
        try:
            pdf = fetch_pdf(
                source, cache_dir,
                timeout_s=getattr(ft, "timeout_s", 90),
                max_mb=getattr(ft, "max_mb", 30),
                retries=getattr(ft, "retries", 2),
            )
        except FulltextError as exc:
            raise ToolError(f"read_pdf: {exc}") from exc
        return extract_pdf_text(str(pdf), max_chars)
    p = _safe_resolve(source, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
    return extract_pdf_text(str(p), max_chars)


def _fetch_paper_fulltext(arguments: dict, ctx: ToolCallContext) -> str:
    """按 DOI/标题取论文全文——审判端读全文的主力入口（v1.0.6-rev3）。

    发现链：本地库(opt-in) → OpenAlex → arXiv → Unpaywall → PMC → S2；
    逐源容错，全败逐源报因。来源与字符数随返回头给出。
    """
    from haa.llm.fulltext import FulltextError, get_fulltext

    doi = str(arguments.get("doi", "")).strip() or None
    title = str(arguments.get("title", "")).strip() or None
    if not doi and not title:
        raise ToolError("fetch_paper_fulltext: 'doi' or 'title' is required")
    max_chars = int(arguments.get("max_chars") or 30000)

    ft = getattr(ctx.config, "fulltext", None)
    lib = getattr(ft, "local_library", None)
    lib_dir = None
    if lib is not None and getattr(lib, "enabled", False) and getattr(lib, "dir", ""):
        lib_dir = lib.dir
    cache_dir = Path(ctx.campaigns_dir) / (ctx.campaign_id or "_adhoc") / "knowledge" / "fulltext"
    try:
        result = get_fulltext(
            doi=doi, title=title,
            cache_dir=cache_dir, max_chars=max_chars,
            timeout_s=getattr(ft, "timeout_s", 90),
            max_mb=getattr(ft, "max_mb", 30),
            retries=getattr(ft, "retries", 2),
            local_library_dir=lib_dir,
            email=str(getattr(ft, "email", "") or ""),
        )
    except FulltextError as exc:
        raise ToolError(str(exc)) from exc
    header = (
        f"[fulltext | source={result.source} method={result.method} "
        f"chars={result.chars} origin={result.url_or_path}]\n\n"
    )
    return header + result.text


def _describe_image(arguments: dict, ctx: ToolCallContext) -> str:
    """调用配置的视觉模型描述论文插图（vision 段随 HAA_CONFIG 生效）。"""
    from haa.config import default_config
    from haa.llm.vision import VisionError, describe_image as _desc

    image_path = arguments.get("image_path", "")
    image_url = arguments.get("image_url", "")
    prompt = arguments.get(
        "prompt",
        "详细描述这张图片的内容，包括图表数据、架构、流程等所有可见信息。",
    )
    if not image_path and not image_url:
        raise ToolError("describe_image: 'image_path' or 'image_url' is required")
    if image_path:
        image_path = str(
            _safe_resolve(image_path, ctx.campaign_id, ctx.campaigns_dir, ctx.allowed_roots)
        )
    try:
        return _desc(
            image_path=image_path or None,
            image_url=image_url or None,
            prompt=prompt,
            config=ctx.vision_config or default_config().vision,
        )
    except VisionError as exc:
        raise ToolError(str(exc)) from exc


def _result_visualization(arguments: dict, ctx: ToolCallContext) -> str:
    """生成实验结果图表 + GLM-4.6V 交叉验证。"""
    import json as _json

    from haa.config import default_config
    from haa.tools.visualization import generate_chart, verify_chart

    code = str(arguments.get("code", ""))
    expected = str(arguments.get("expected_description", ""))
    filename = str(arguments.get("filename", "chart.png"))
    if not code:
        raise ToolError("result_visualization: 'code' is required")
    output_path = (
        str(ctx.campaigns_dir / ctx.campaign_id / "figures" / filename)
        if ctx.campaign_id
        else f"/tmp/{filename}"
    )
    result = generate_chart(code, output_path)
    if not result["success"]:
        return f"图表生成失败: {result['error']}"
    if expected:
        v = verify_chart(result["image_path"], expected, ctx.vision_config or default_config().vision)
        return _json.dumps(
            {
                "image_path": result["image_path"],
                "verified": v["verified"],
                "description": v["description"],
                "issues": v["issues"],
            },
            ensure_ascii=False,
            indent=2,
        )
    return _json.dumps(
        {"image_path": result["image_path"], "verified": False,
         "note": "未提供 expected_description，跳过验证"},
        ensure_ascii=False,
    )


def _illustration_drawing(arguments: dict, ctx: ToolCallContext) -> str:
    """调 nano-banana-2 生成论文插图。"""
    from haa.tools.illustration import IllustrationError, generate_illustration

    prompt = str(arguments.get("prompt", ""))
    filename = str(arguments.get("filename", "illustration.png"))
    aspect_ratio = str(arguments.get("aspect_ratio", "16:9"))
    if not prompt:
        raise ToolError("illustration_drawing: 'prompt' is required")
    output_path = (
        str(ctx.campaigns_dir / ctx.campaign_id / "figures" / filename)
        if ctx.campaign_id
        else f"/tmp/{filename}"
    )
    try:
        path = generate_illustration(prompt, output_path, aspect_ratio)
        return f"Illustration generated and saved to: {path}"
    except IllustrationError as exc:
        raise ToolError(str(exc)) from exc


# ===========================================================================
# Tool definitions (schemas with complete `required` — HM-Pro Lesson 6)
# ===========================================================================


def _default_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="grep",
            description=(
                "Search file CONTENTS with a regex under the campaign dir "
                "(knowledge files, artifacts, scripts). Returns 'file:line: "
                "match' entries. Prefer this over reading whole files blindly: "
                "first grep to locate the relevant sections, then read_file "
                "with a narrower scope."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex to search for."},
                    "path": {"type": "string", "description": "Optional subdir or file (relative to campaign dir) to narrow the search."},
                    "max_matches": {"type": "integer", "description": "Max matches to return (default 50, cap 200).", "default": 50},
                },
                "required": ["pattern"],
            },
            allowed_stages=frozenset(
                {"SEEK", "NOVELTY", "SCREEN", "DESIGN", "VERIFY", "GRADE", "WRITE",
                 "REVIEW", "REFINE", "EXP_SPEC", "EXP_FEASIBILITY"}
            ),
            handler=_grep,
        ),
        ToolDefinition(
            name="fetch_paper_fulltext",
            description=(
                "Fetch the FULL TEXT of a paper by DOI or title. Discovery chain: "
                "local library (if enabled) → OpenAlex → arXiv → Unpaywall → PMC → "
                "Semantic Scholar. MANDATORY before judging a paper's feasibility "
                "or novelty from it — never assert claims from the abstract alone. "
                "Returns the extracted text with a source header."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "doi": {"type": "string", "description": "Paper DOI (e.g. 10.1038/s41586-020-2649-2)."},
                    "title": {"type": "string", "description": "Paper title (used when DOI unknown)."},
                    "max_chars": {"type": "integer", "description": "Max chars to return (default 30000).", "default": 30000},
                },
                "required": [],
            },
            allowed_stages=frozenset(
                {"SEEK", "NOVELTY", "SCREEN", "EXP_SPEC", "EXP_FEASIBILITY"}
            ),
            handler=_fetch_paper_fulltext,
        ),
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web for up-to-date information. Returns a list of "
                "results with title, url, and a short snippet each."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            allowed_stages=frozenset({"SEEK", "NOVELTY", "SCREEN", "VERIFY", "WRITE", "EXP_SPEC", "EXP_FEASIBILITY"}),
            handler=_web_search,
        ),
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch the content of a URL and return its main text (HTML is "
                "stripped). Use for reading a specific page found via web_search."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The absolute URL to fetch."},
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return.",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
            allowed_stages=frozenset({"SEEK", "NOVELTY", "WRITE", "EXP_SPEC", "EXP_FEASIBILITY"}),
            handler=_web_fetch,
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "Read a text file from the campaign working directory. Paths are "
                "sandboxed: they must stay inside the campaign dir."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to the campaign dir, or absolute within it).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return.",
                        "default": 50000,
                    },
                },
                "required": ["path"],
            },
            allowed_stages=frozenset(
                {"SEEK", "SCREEN", "DESIGN", "VERIFY", "GRADE", "WRITE", "REVIEW", "REFINE",
                 "EXP_SPEC", "EXP_FEASIBILITY"}
            ),
            handler=_read_file,
        ),
        ToolDefinition(
            name="write_file",
            description=(
                "Write text to a file inside the campaign working directory "
                "(creates parent dirs). Used by WRITE/REFINE to save paper sections."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to the campaign dir, or absolute within it).",
                    },
                    "content": {"type": "string", "description": "The full text to write."},
                },
                "required": ["path", "content"],
            },
            allowed_stages=frozenset({"SCREEN", "DESIGN", "WRITE", "REFINE", "EXP_SPEC"}),
            handler=_write_file,
        ),
        ToolDefinition(
            name="exec_bash",
            description=(
                "Run a shell command in the campaign working directory. Dangerous "
                "patterns are blocked; the command is killed after a timeout. Use "
                "to run code that constructs separating instances or checks claims."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30).",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            allowed_stages=frozenset({"SCREEN", "VERIFY"}),
            handler=_exec_bash,
        ),
        ToolDefinition(
            name="search_paper",
            description=(
                "Search academic papers (Semantic Scholar). Returns title, year, "
                "citation count, and a short abstract for each. Use to check "
                "novelty / find related work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The paper search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of papers to return.",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            allowed_stages=frozenset({"SEEK", "NOVELTY", "EXP_SPEC", "EXP_FEASIBILITY"}),
            handler=_search_paper,
        ),
        ToolDefinition(
            name="edit_file",
            description=(
                "Precisely replace a unique string fragment in a file with a new one "
                "(in-place, unlike write_file which overwrites the whole file). "
                "old_string must match exactly once unless replace_all=true. Use for "
                "targeted edits to paper sections or scripts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to the campaign dir)."},
                    "old_string": {"type": "string", "description": "The exact text to replace (must be unique, or set replace_all)."},
                    "new_string": {"type": "string", "description": "The replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false.", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
            allowed_stages=frozenset({"SCREEN", "DESIGN", "WRITE", "REFINE"}),
            handler=_edit_file,
        ),
        ToolDefinition(
            name="to_do_write",
            description=(
                "Record a step-by-step task list for the current stage. Use to plan "
                "multi-step work (a proof into obligations, a paper into sections) and "
                "track progress. Each item is an object {content, status, active_form}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Todo items (objects, or plain strings).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                "active_form": {"type": "string"},
                            },
                            "required": ["content"],
                        },
                    }
                },
                "required": ["todos"],
            },
            allowed_stages=frozenset({"DESIGN", "SCREEN", "VERIFY", "WRITE", "REFINE", "EXP_SPEC", "EXP_FEASIBILITY"}),
            handler=_to_do_write,
        ),
        ToolDefinition(
            name="multi_agents",
            description=(
                "Fan out several independent sub-agents in parallel, each a separate "
                "LLM call (no tools, no recursion). Give each a role and a prompt. Use "
                "for multi-angle work: SEEK (ideas from different sub-fields), SCREEN "
                "(attack a candidate from several angles), REVIEW-style panels."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "Sub-agent tasks (max 6).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "description": "What this sub-agent focuses on."},
                                "prompt": {"type": "string", "description": "The instruction for this sub-agent."},
                            },
                            "required": ["prompt"],
                        },
                    }
                },
                "required": ["tasks"],
            },
            allowed_stages=frozenset({"SEEK", "NOVELTY", "SCREEN", "REVIEW"}),
            handler=_multi_agents,
        ),
        ToolDefinition(
            name="calculator",
            description=(
                "Perform a computation — ALWAYS use this for non-trivial math, do NOT "
                "compute in your head. Three modes: 'numeric' (a Python expression, e.g. "
                "2**10+3*5); 'symbolic' (SymPy, optional verb simplify/expand/factor/diff/"
                "integrate/solve then an expression); 'search_counterexample' (a Python "
                "bool expression over variables + params giving each variable's range; "
                "reports points where the claim is False)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["numeric", "symbolic", "search_counterexample"]},
                    "expression": {
                        "type": "string",
                        "description": "numeric: Python expr; symbolic: SymPy expr (optional verb prefix); counterexample: a Python bool expression (the claim).",
                    },
                    "params": {
                        "type": "object",
                        "description": "counterexample mode only: per-variable ranges. Each value is [lo,hi,step] or a list of values.",
                    },
                },
                "required": ["mode", "expression"],
            },
            allowed_stages=frozenset({"SCREEN", "DESIGN", "VERIFY", "GRADE", "EXP_SPEC", "EXP_FEASIBILITY"}),
            handler=_calculator,
        ),
        ToolDefinition(
            name="read_pdf",
            description=(
                "Extract text from a PDF file (local path or URL). Uses pdftotext "
                "for layout-preserving extraction. Use to read academic papers, "
                "technical reports, or any PDF."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "URL or local file path of the PDF."},
                    "max_chars": {"type": "integer", "description": "Max chars to return (default 50000).", "default": 50000},
                },
                "required": ["source"],
            },
            allowed_stages=frozenset({
                "SEEK", "NOVELTY", "SCREEN", "DESIGN", "VERIFY",
                "GRADE", "EXP_SPEC", "EXP_FEASIBILITY",
                "WRITE", "REVIEW", "REFINE",
            }),
            handler=_read_pdf,
        ),
        ToolDefinition(
            name="describe_image",
            description=(
                "Describe an image using the configured vision model. Use to "
                "understand figures, diagrams, charts from papers. Provide a "
                "local file path (within campaign dir) or an image URL."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Local file path (within campaign dir)."},
                    "image_url": {"type": "string", "description": "URL of the image."},
                    "prompt": {"type": "string", "description": "What to focus on.", "default": "详细描述这张图片的内容..."},
                },
                "required": [],
            },
            allowed_stages=frozenset({
                "SEEK", "NOVELTY", "SCREEN", "DESIGN",
                "EXP_SPEC", "EXP_FEASIBILITY",
                "WRITE", "REVIEW", "REFINE",
            }),
            handler=_describe_image,
        ),
        ToolDefinition(
            name="result_visualization",
            description=(
                "Generate a data visualization chart from matplotlib/seaborn code "
                "and optionally cross-verify it with the configured vision model. "
                "Provide the code plus a description of what the chart should show."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python matplotlib/seaborn code."},
                    "expected_description": {"type": "string", "description": "What the chart should show (for verification)."},
                    "filename": {"type": "string", "description": "Output filename (default chart.png).", "default": "chart.png"},
                },
                "required": ["code"],
            },
            allowed_stages=frozenset({"WRITE", "REFINE"}),
            handler=_result_visualization,
        ),
        ToolDefinition(
            name="illustration_drawing",
            description=(
                "Generate a professional academic paper illustration using "
                "nano-banana-2 AI image generation. Provide a detailed description "
                "of the desired illustration (architecture diagrams, concept figures)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Description of the illustration to generate."},
                    "filename": {"type": "string", "description": "Output filename (default illustration.png).", "default": "illustration.png"},
                    "aspect_ratio": {"type": "string", "description": "16:9, 1:1, 4:3, 3:4 (default 16:9).", "default": "16:9"},
                },
                "required": ["prompt"],
            },
            allowed_stages=frozenset({"WRITE", "REFINE"}),
            handler=_illustration_drawing,
        ),
    ]
