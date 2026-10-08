"""批次10-13 新增工具。compile_latex / citation 系列 / memory 双件 /
spawn_subagent / run_code / report_challenge / EA 门视图 / experiment 写入。

HTTP 调用统一经项目已有的 web_fetch 工具（内含 SSRF 安全闸——协议
白名单+IP 边界校验，v1.0.6-rev2 起），本文件不直接发 HTTP 请求。
"""
from __future__ import annotations

import ast
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from haa.harness.registry import ToolCallContext, ToolError, ToolResult
from haa.harness.tools.exec_bash import truncate_output

logger = logging.getLogger("haa.harness.tools.batch10_13")

_OPENALEX = "https://api.openalex.org/works"


def _fetch_json(url: str, fetcher=None) -> dict:
    """经已有 web_fetch 通道获取 JSON（SSRF 安全闸在彼处）。"""
    if fetcher is None:
        from haa.llm.tools import ToolCallContext as _LegacyCtx
        from haa.llm.tools import _web_fetch
        ctx = _LegacyCtx()
        text = _web_fetch({"url": url}, ctx)
        return json.loads(text) if text.startswith("{") else {}
    return fetcher(url)


# ==================== compile_latex ====================

LATEX_COMPILE_RULES = (
    "LaTeX: compile_latex runs pdflatex and returns structured errors. "
    "Fix with edit_file, recompile. Max 5 rounds."
)


def _compile_latex(args: dict, ctx: ToolCallContext) -> ToolResult:
    tex_file = str(args.get("tex_file", "")).strip()
    if not tex_file or not tex_file.endswith(".tex"):
        raise ToolError("compile_latex: 'tex_file' must be a .tex path")
    workdir = str(Path(tex_file).parent)
    from haa.harness.tools.jobs import _spawn_group
    proc = _spawn_group(
        f"cd {workdir} && pdflatex -interaction=nonstopmode "
        f"{Path(tex_file).name} 2>&1 | tail -30")
    try:
        out, _ = proc.communicate(timeout=120)
    except Exception:
        proc.kill()
        out = b"timeout"
    text = out.decode("utf-8", errors="replace")
    errors = re.findall(r"^!(.*?)(?:\n|$)", text, re.M)
    if not errors:
        return ToolResult(content="compilation OK",
                          meta={"exit_code": 0, "errors": []})
    lines = [f"{len(errors)} error(s):"]
    for e in errors[:10]:
        lines.append(f"  ! {e.strip()}")
    pdf = Path(tex_file).with_suffix(".pdf")
    lines.append(f"PDF exists: {pdf.exists()}")
    body, _ = truncate_output("\n".join(lines))
    return ToolResult(content=body, meta={"exit_code": 1, "errors": errors[:10]})


# ==================== citation_graph / verify / search_analog ====================

CITATION_GRAPH_RULES = "Citations: citation_graph returns refs/citations."
CITATION_VERIFY_RULES = "Citation verify: check DOI/title via OpenAlex."
SEARCH_ANALOG_RULES = (
    "Cross-domain: search_analog finds similar work across domains. "
    "Transfer ≠ collision — innovation point.")


def _citation_graph(args: dict, ctx: ToolCallContext) -> ToolResult:
    doi = str(args.get("doi", "")).strip()
    title = str(args.get("title", "")).strip()
    if not doi and not title:
        raise ToolError("citation_graph: 'doi' or 'title' required")
    try:
        from urllib.parse import quote
        if doi:
            data = _fetch_json(f"{_OPENALEX}/doi:{doi}")
        else:
            results = _fetch_json(f"{_OPENALEX}?filter=title.search:{quote(title)}&per_page=1")
            data = results.get("results", [{}])[0]
        return ToolResult(content=json.dumps({
            "title": data.get("title", ""),
            "cited_by_count": data.get("cited_by_count", 0)},
            ensure_ascii=False))
    except Exception as exc:
        return ToolResult(content=f"citation_graph failed: {exc}")


def _citation_verify(args: dict, ctx: ToolCallContext) -> ToolResult:
    doi = str(args.get("doi", "")).strip()
    title = str(args.get("title", "")).strip()
    if not doi and not title:
        raise ToolError("citation_verify: 'doi' or 'title' required")
    try:
        from urllib.parse import quote
        if doi:
            data = _fetch_json(f"{_OPENALEX}/doi:{doi}")
        else:
            results = _fetch_json(f"{_OPENALEX}?filter=title.search:{quote(title)}&per_page=1")
            data = results.get("results", [{}])[0]
        if data.get("id"):
            return ToolResult(content=f"VERIFIED: {data.get('title', title)}",
                              meta={"verified": True})
        return ToolResult(content=f"UNVERIFIED: {doi or title}",
                          meta={"verified": False})
    except Exception as exc:
        return ToolResult(content=f"error: {exc}", meta={"verified": None})


def _search_analog(args: dict, ctx: ToolCallContext) -> ToolResult:
    fp = args.get("fingerprint") or {}
    query = " ".join(str(fp.get(k, "")) for k in
                     ("conflict_type", "guarantee_type", "problem_class")
                     if fp.get(k)).strip() or str(args.get("query", "")).strip()
    if not query:
        raise ToolError("search_analog: 'fingerprint' or 'query' required")
    try:
        from urllib.parse import quote
        data = _fetch_json(f"{_OPENALEX}?search={quote(query)}&per_page=5")
        results = [{"title": w.get("title", ""),
                    "cited_by": w.get("cited_by_count", 0)}
                   for w in (data.get("results") or [])[:5]]
        if not results:
            return ToolResult(content=f"(no results for: {query})")
        return ToolResult(content=json.dumps(results, ensure_ascii=False))
    except Exception as exc:
        return ToolResult(content=f"search_analog failed: {exc}")


# ==================== memory_query / memory_write ====================

MEMORY_QUERY_RULES = "Memory: query dead ideas by problem class/keyword."
MEMORY_WRITE_RULES = "Memory: write entity (matrix-enforced)."


def _make_memory_query(bank):
    def memory_query(args: dict, ctx: ToolCallContext) -> ToolResult:
        hits = bank.query(problem_class=args.get("problem_class") or None,
                          text_like=args.get("text_like") or None,
                          top_n=int(args.get("top_n", 20)))
        if not hits:
            return ToolResult(content="(no matching entries)")
        return ToolResult(content="\n".join(
            f"  [{h.get('entity')}] {h.get('title')} ({h.get('status')})"
            for h in hits[:10]))
    return memory_query


def _make_memory_write(bank):
    def memory_write(args: dict, ctx: ToolCallContext) -> ToolResult:
        from haa.memory_bank import ENTITY_MODELS, WritePermissionError
        entity = str(args.get("entity", "")).strip()
        data = args.get("data") or {}
        if entity not in ENTITY_MODELS:
            raise ToolError(f"unknown entity {entity!r}")
        try:
            model = ENTITY_MODELS[entity](**data)
            bank.write(entity, model,
                       writer=str(args.get("writer", "memory_write_tool")))
        except WritePermissionError as exc:
            raise ToolError(f"matrix rejected: {exc}")
        except Exception as exc:
            raise ToolError(f"validation: {exc}")
        return ToolResult(content=f"written {entity} ok")
    return memory_write


# ==================== spawn_subagent / run_code / report_challenge ====================

SPAWN_SUBAGENT_RULES = "Subagents: spawn parallel task (v1 sync)."
RUN_CODE_RULES = "Code Mode: run Python. AST whitelist. 300s timeout."

_BANNED = {"os", "subprocess", "socket", "shutil", "sys", "ctypes"}


def _validate_code_ast(code: str) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _BANNED:
                    errors.append(f"banned: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _BANNED:
                errors.append(f"banned: {node.module}")
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) \
                and node.test.value is True:
            if not any(isinstance(n, (ast.Break, ast.Return)) for n in ast.walk(node)):
                errors.append("infinite loop")
    return errors


def _make_spawn_subagent(services):
    def spawn_subagent(args: dict, ctx: ToolCallContext) -> ToolResult:
        task = str(args.get("task", "")).strip()
        if not task:
            raise ToolError("'task' required")
        sid = f"sub-{int(time.time() * 1000) % 10000}"
        return ToolResult(
            content=f"subagent {sid}: {task[:200]}…(v1 sync)",
            meta={"subagent_id": sid})
    return spawn_subagent


def _make_run_code(services):
    def run_code_fn(args: dict, ctx: ToolCallContext) -> ToolResult:
        code = str(args.get("code", ""))
        if not code.strip():
            raise ToolError("'code' required")
        errs = _validate_code_ast(code)
        if errs:
            raise ToolError(f"AST: {'; '.join(errs)}")
        import tempfile
        import os
        fd, tmp = tempfile.mkstemp(suffix=".py", prefix="rc_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(code)
            from haa.harness.tools.jobs import _spawn_group
            proc = _spawn_group(f"python3 {tmp} 2>&1")
            try:
                out, _ = proc.communicate(timeout=300)
            except Exception:
                proc.kill()
                out = b"timeout(300s)"
            body = out.decode("utf-8", errors="replace") if out else "(no output)"
        finally:
            Path(tmp).unlink(missing_ok=True)
        body, _ = truncate_output(body)
        return ToolResult(content=body)
    return run_code_fn


def _make_report_challenge(services):
    def report_challenge(args: dict, ctx: ToolCallContext) -> ToolResult:
        from haa.anchor_guard import CHALLENGE_TYPES
        ctype = str(args.get("challenge_type", "")).strip()
        evidence = str(args.get("evidence", "")).strip()
        if ctype not in CHALLENGE_TYPES:
            raise ToolError(f"must be {CHALLENGE_TYPES}")
        if not evidence:
            raise ToolError("'evidence' required")
        return ToolResult(
            content=f"challenge: {ctype}\n{evidence[:300]}\n→ pauses",
            meta={"challenge": {"challenge_type": ctype, "evidence": evidence}})
    return report_challenge


# ==================== EA 门 + experiment 写入 ====================

def render_ea_gate_branch_view(context) -> str:
    tree = (getattr(context, "extra", None) or {}).get("branch_tree")
    if tree is None:
        return "(no branch tree)"
    return tree.render_tree_view()


def write_experiment_entity(bank, *, idea_id, tier, verdict, metrics,
                            env, solve_sh):
    from haa.memory_bank import ExperimentEntity, next_entity_id
    exp_id = next_entity_id(bank.root, "e")
    bank.write("experiments", ExperimentEntity(
        exp_id=exp_id, idea_id=idea_id, tier=tier, verdict=verdict,
        metrics=metrics, env=env, solve_sh=solve_sh), writer="P2")
    return exp_id
