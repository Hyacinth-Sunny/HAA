"""Tests for the tool layer (haa/llm/tools.py).

Covers: registry stage-filtering + permission enforcement, JSON-schema
completeness (HM-Pro Lesson 6), the file path sandbox (escape/symlink),
exec_bash command filtering + timeout, and the three network tools with the
HTTP helpers monkeypatched (no network).
"""

from __future__ import annotations

import os

import pytest

from haa.config import load_config
from haa.llm import tools as tools_mod
from haa.llm.tools import (
    ALL_STAGES,
    ToolError,
    ToolPermissionError,
    ToolRegistry,
    ToolSafetyError,
)


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def cfg():
    # Use the real default.yaml tools section so the YAML wiring is exercised.
    return load_config().tools


@pytest.fixture
def registry(tmp_path, cfg):
    # campaigns_dir = tmp_path; NO extra allowed_roots so the campaign dir is the
    # only sandbox root and escape attempts are caught.
    return ToolRegistry(cfg, campaigns_dir=tmp_path, allowed_roots=[])


CAMP = "camp1"


# --- registry: filtering + permissions --------------------------------------

def test_default_tools_registered(registry):
    assert registry.names() == [
        "calculator", "describe_image", "edit_file", "exec_bash",
        "fetch_paper_fulltext", "grep", "illustration_drawing", "multi_agents",
        "read_file", "read_pdf", "result_visualization", "search_paper",
        "to_do_write", "web_fetch", "web_search", "write_file",
    ]


def test_get_schemas_filters_by_stage(registry):
    def names(stage):
        return sorted(s["function"]["name"] for s in registry.get_schemas(stage))

    assert names("SEEK") == ["describe_image", "fetch_paper_fulltext", "grep", "multi_agents", "read_file", "read_pdf", "search_paper", "web_fetch", "web_search"]
    assert names("NOVELTY") == ["describe_image", "fetch_paper_fulltext", "grep", "multi_agents", "read_pdf", "search_paper", "web_fetch", "web_search"]
    assert names("SCREEN") == ["calculator", "describe_image", "edit_file", "exec_bash", "fetch_paper_fulltext", "grep", "multi_agents", "read_file", "read_pdf", "to_do_write", "web_search", "write_file"]
    assert names("DESIGN") == ["calculator", "describe_image", "edit_file", "grep", "read_file", "read_pdf", "to_do_write", "write_file"]
    assert names("VERIFY") == ["calculator", "exec_bash", "grep", "read_file", "read_pdf", "to_do_write", "web_search"]
    assert names("GRADE") == ["calculator", "grep", "read_file", "read_pdf"]
    assert names("WRITE") == ["describe_image", "edit_file", "grep", "illustration_drawing", "read_file", "read_pdf", "result_visualization", "to_do_write", "web_fetch", "web_search", "write_file"]
    assert names("REVIEW") == ["describe_image", "grep", "multi_agents", "read_file", "read_pdf"]
    assert names("REFINE") == ["describe_image", "edit_file", "grep", "illustration_drawing", "read_file", "read_pdf", "result_visualization", "to_do_write", "write_file"]


def test_every_schema_has_required_field(registry):
    """HM-Pro Lesson 6: structured-output schemas must have complete `required`."""
    for tool in registry.get_schemas("SCREEN"):
        params = tool["function"]["parameters"]
        assert params["type"] == "object"
        assert "required" in params, f"{tool['function']['name']} missing required"
        assert isinstance(params["required"], list)


def test_permission_blocks_disallowed_stage(registry):
    with pytest.raises(ToolPermissionError):
        registry.execute(
            "write_file", {"path": "x.txt", "content": "y"},
            stage_name="SEEK", campaign_id=CAMP,
        )


def test_permission_blocks_exec_bash_in_wrong_stage(registry):
    with pytest.raises(ToolPermissionError):
        registry.execute(
            "exec_bash", {"command": "echo hi"},
            stage_name="SEEK", campaign_id=CAMP,
        )


def test_unknown_tool_raises(registry):
    with pytest.raises(ToolError):
        registry.execute("no_such_tool", {}, stage_name="SEEK")


def test_is_allowed_helpers(registry):
    assert registry.is_allowed("web_search", "SEEK") is True
    assert registry.is_allowed("web_search", "GRADE") is False
    assert registry.is_allowed("read_file", "GRADE") is True  # ALL_STAGES
    assert registry.is_allowed("ghost", "SEEK") is False


# --- read_file / write_file sandbox -----------------------------------------

def test_write_then_read_inside_campaign(registry):
    out = registry.execute(
        "write_file", {"path": "notes.txt", "content": "hello world"},
        stage_name="WRITE", campaign_id=CAMP,
    )
    assert "wrote" in out
    content = registry.execute(
        "read_file", {"path": "notes.txt"},
        stage_name="GRADE", campaign_id=CAMP,  # read_file allowed everywhere
    )
    assert content == "hello world"


def test_write_in_subdir(registry):
    registry.execute(
        "write_file", {"path": "sections/intro.md", "content": "# Intro"},
        stage_name="WRITE", campaign_id=CAMP,
    )
    assert "# Intro" == registry.execute(
        "read_file", {"path": "sections/intro.md"},
        stage_name="GRADE", campaign_id=CAMP,
    )


def test_read_missing_file_errors(registry):
    with pytest.raises(ToolError):
        registry.execute("read_file", {"path": "nope.txt"}, stage_name="GRADE", campaign_id=CAMP)


def test_write_escape_blocked(registry):
    with pytest.raises(ToolSafetyError):
        registry.execute(
            "write_file", {"path": "../evil.txt", "content": "x"},
            stage_name="WRITE", campaign_id=CAMP,
        )


def test_read_absolute_escape_blocked(registry):
    with pytest.raises(ToolSafetyError):
        registry.execute(
            "read_file", {"path": "/etc/passwd"},
            stage_name="GRADE", campaign_id=CAMP,
        )


def test_read_symlink_escape_blocked(registry, tmp_path):
    """A symlink inside the campaign dir pointing outside must be rejected."""
    target = tmp_path / "secret.txt"
    target.write_text("top-secret")
    camp_dir = tmp_path / CAMP
    camp_dir.mkdir(parents=True)
    link = camp_dir / "escape.txt"
    try:
        os.symlink(target, link)
    except OSError:  # symlinks unsupported on this FS
        pytest.skip("symlinks not supported")
    with pytest.raises(ToolSafetyError):
        registry.execute("read_file", {"path": "escape.txt"}, stage_name="GRADE", campaign_id=CAMP)


def test_read_file_truncates(registry, cfg):
    big = "A" * 1000
    registry.execute("write_file", {"path": "big.txt", "content": big}, stage_name="WRITE", campaign_id=CAMP)
    out = registry.execute("read_file", {"path": "big.txt", "max_chars": 10}, stage_name="GRADE", campaign_id=CAMP)
    assert out.endswith("…[truncated]")
    assert len(out) < 1000


# --- exec_bash ---------------------------------------------------------------

def test_exec_bash_returns_output(registry):
    out = registry.execute(
        "exec_bash", {"command": "echo hello && echo world"},
        stage_name="VERIFY", campaign_id=CAMP,
    )
    assert "hello" in out and "world" in out


def test_exec_bash_blocked_command(registry):
    with pytest.raises(ToolSafetyError):
        registry.execute("exec_bash", {"command": "rm -rf /"}, stage_name="VERIFY", campaign_id=CAMP)


def test_exec_bash_blocked_sudo(registry):
    # VERIFY allows exec_bash; the command filter must still block `sudo`.
    with pytest.raises(ToolSafetyError):
        registry.execute("exec_bash", {"command": "sudo ls"}, stage_name="VERIFY", campaign_id=CAMP)


def test_exec_bash_timeout(registry):
    # Pass an explicit 1s timeout (overrides the config default) so the test is
    # fast; the command sleeps longer and must be killed.
    with pytest.raises(ToolError):
        registry.execute(
            "exec_bash", {"command": "sleep 5", "timeout": 1},
            stage_name="VERIFY", campaign_id=CAMP,
        )


def test_exec_bash_permission(registry):
    with pytest.raises(ToolPermissionError):
        registry.execute("exec_bash", {"command": "echo x"}, stage_name="SEEK", campaign_id=CAMP)


# --- web_search (HTTP monkeypatched) ----------------------------------------

def test_web_search_without_key_returns_message(registry, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = registry.execute("web_search", {"query": "q"}, stage_name="SEEK")
    assert "TAVILY_API_KEY" in out  # graceful, not a crash


def test_web_search_with_key_parses_results(registry, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    captured = {}

    def fake_post_json(url, *, json_body=None, headers=None, timeout=30):
        captured["url"] = url
        captured["body"] = json_body
        return {"results": [
            {"title": "Paper A", "url": "http://a", "content": "blah"},
            {"title": "Paper B", "url": "http://b", "content": "more"},
        ]}

    monkeypatch.setattr(tools_mod, "_http_post_json", fake_post_json)
    out = registry.execute("web_search", {"query": "transformers"}, stage_name="SEEK")
    assert "Paper A" in out and "http://a" in out
    assert captured["body"]["query"] == "transformers"
    assert captured["body"]["api_key"] == "fake-key"


# --- web_fetch (HTTP monkeypatched) -----------------------------------------

def test_web_fetch_strips_html(registry, monkeypatch):
    def fake_get_text(url, *, params=None, headers=None, timeout=30):
        return "<html><body><script>x</script><h1>Title</h1><p>Body text</p></body></html>"

    monkeypatch.setattr(tools_mod, "_http_get_text", fake_get_text)
    out = registry.execute("web_fetch", {"url": "http://x"}, stage_name="SEEK")
    assert "Title" in out and "Body text" in out
    assert "<script>" not in out and "<h1>" not in out  # tags/script stripped


def test_web_fetch_requires_url(registry):
    with pytest.raises(ToolError):
        registry.execute("web_fetch", {}, stage_name="SEEK")


# --- search_paper (HTTP monkeypatched) --------------------------------------

def test_search_paper_parses_results(registry, monkeypatch):
    captured = {}

    def fake_get_json(url, *, params=None, headers=None, timeout=30):
        captured["url"] = url
        captured["params"] = params
        return {"data": [
            {"title": "Attention Is All You Need", "year": 2017,
             "citationCount": 99999, "externalIds": {"DOI": "10.1/x"},
             "abstract": "Transformer architecture"},
        ]}

    monkeypatch.setattr(tools_mod, "_http_get_json", fake_get_json)
    out = registry.execute("search_paper", {"query": "attention"}, stage_name="NOVELTY")
    assert "Attention Is All You Need" in out
    assert "2017" in out and "99999" in out
    assert captured["params"]["query"] == "attention"


# --- v1.0: search_paper fallback chain (S2 → OpenAlex → arXiv) ---------------

def test_search_paper_falls_back_to_openalex(registry, monkeypatch):
    """S2 fails (exception) → OpenAlex responds → results returned with cites."""
    calls = []

    def fake_get_json(url, *, params=None, headers=None, timeout=30):
        calls.append(url)
        if "semanticscholar" in url:
            raise ConnectionError("429 rate limited")
        assert "openalex" in url
        return {"results": [{
            "display_name": "GPTuner Paper", "publication_year": 2024,
            "cited_by_count": 42, "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1/y",
            # Real OpenAlex shape: {word: [positions]}.
            "abstract_inverted_index": {"Knob": [0], "tuning": [1], "LLM": [3]},
        }]}

    monkeypatch.setattr(tools_mod, "_http_get_json", fake_get_json)
    out = registry.execute("search_paper", {"query": "knob tuning"}, stage_name="NOVELTY")
    assert "GPTuner Paper" in out
    assert "cites=42" in out
    assert "Knob tuning LLM" in out  # inverted index rebuilt
    assert len(calls) == 2  # S2 tried first, then OpenAlex


def test_search_paper_falls_back_to_arxiv(registry, monkeypatch):
    """S2 empty + OpenAlex fails → arXiv Atom XML parsed."""

    def fake_get_json(url, *, params=None, headers=None, timeout=30):
        if "semanticscholar" in url:
            return {"data": []}  # empty → triggers fallback
        raise ConnectionError("openalex down")

    import urllib.request as _ur
    from unittest.mock import patch as _patch

    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>Federated Tuning  </title><published>2023-05-01T00:00:00Z</published>"
        "<id>http://arxiv.org/abs/2305.1</id><summary> We tune federated. </summary></entry>"
        "</feed>"
    )

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tools_mod, "_http_get_json", fake_get_json)
    with _patch.object(_ur, "urlopen", return_value=FakeResp(atom)):
        out = registry.execute("search_paper", {"query": "federated tuning"}, stage_name="NOVELTY")
    assert "Federated Tuning" in out
    assert "2305.1" in out
    assert "preprint" in out


def test_search_paper_all_sources_failed(registry, monkeypatch):
    """All three sources fail → tool error naming the failure."""

    def fake_get_json(url, *, params=None, headers=None, timeout=30):
        raise ConnectionError("down")

    import urllib.request as _ur
    from unittest.mock import patch as _patch

    monkeypatch.setattr(tools_mod, "_http_get_json", fake_get_json)
    with _patch.object(_ur, "urlopen", side_effect=ConnectionError("down")):
        out = registry.execute("search_paper", {"query": "x"}, stage_name="NOVELTY")
    assert "all sources failed" in out


# --- Phase v0.1 tools: edit_file / to_do_write / calculator / multi_agents ----

def test_edit_file_replaces_unique(registry):
    registry.execute("write_file", {"path": "e.txt", "content": "alpha beta gamma"}, stage_name="WRITE", campaign_id=CAMP)
    out = registry.execute("edit_file", {"path": "e.txt", "old_string": "beta", "new_string": "BETA"}, stage_name="WRITE", campaign_id=CAMP)
    assert "replaced 1" in out
    assert registry.execute("read_file", {"path": "e.txt"}, stage_name="GRADE", campaign_id=CAMP) == "alpha BETA gamma"


def test_edit_file_not_found_errors(registry):
    registry.execute("write_file", {"path": "e2.txt", "content": "xx"}, stage_name="WRITE", campaign_id=CAMP)
    with pytest.raises(ToolError):
        registry.execute("edit_file", {"path": "e2.txt", "old_string": "nope", "new_string": "y"}, stage_name="WRITE", campaign_id=CAMP)


def test_edit_file_ambiguous_without_replace_all(registry):
    registry.execute("write_file", {"path": "e3.txt", "content": "a a a"}, stage_name="WRITE", campaign_id=CAMP)
    with pytest.raises(ToolError):
        registry.execute("edit_file", {"path": "e3.txt", "old_string": "a", "new_string": "b"}, stage_name="WRITE", campaign_id=CAMP)
    registry.execute("edit_file", {"path": "e3.txt", "old_string": "a", "new_string": "b", "replace_all": True}, stage_name="WRITE", campaign_id=CAMP)
    assert registry.execute("read_file", {"path": "e3.txt"}, stage_name="GRADE", campaign_id=CAMP) == "b b b"


def test_edit_file_permission(registry):
    with pytest.raises(ToolPermissionError):
        registry.execute("edit_file", {"path": "x", "old_string": "a", "new_string": "b"}, stage_name="SEEK")


def test_to_do_write_persists(registry):
    import json
    out = registry.execute("to_do_write", {"todos": [{"content": "s1", "status": "completed"}, "s2"]}, stage_name="DESIGN", campaign_id=CAMP)
    assert "2 items" in out and "1 done" in out
    body = registry.execute("read_file", {"path": "todo.json"}, stage_name="DESIGN", campaign_id=CAMP)
    data = json.loads(body)
    assert len(data) == 2 and data[0]["status"] == "completed"


def test_to_do_write_empty_errors(registry):
    with pytest.raises(ToolError):
        registry.execute("to_do_write", {"todos": []}, stage_name="DESIGN")


def test_calculator_numeric(registry):
    assert registry.execute("calculator", {"mode": "numeric", "expression": "2**10 + 3*5"}, stage_name="DESIGN") == "1039"


def test_calculator_symbolic(registry):
    out = registry.execute("calculator", {"mode": "symbolic", "expression": "expand (x+1)**2"}, stage_name="DESIGN")
    assert "x**2" in out and "2*x" in out  # x**2 + 2*x + 1


def test_calculator_counterexample_finds(registry):
    out = registry.execute("calculator", {"mode": "search_counterexample", "expression": "n % 2 == 0", "params": {"n": [1, 2, 3, 5]}}, stage_name="VERIFY")
    assert "COUNTEREXAMPLE FOUND" in out and "'n': 1" in out


def test_calculator_counterexample_none(registry):
    out = registry.execute("calculator", {"mode": "search_counterexample", "expression": "n % 2 == 0", "params": {"n": [2, 4, 6]}}, stage_name="VERIFY")
    assert "no counterexample" in out


def test_calculator_permission(registry):
    with pytest.raises(ToolPermissionError):
        registry.execute("calculator", {"mode": "numeric", "expression": "1+1"}, stage_name="SEEK")


def test_multi_agents_fans_out():
    import tempfile
    from haa.config import ToolsConfig
    from haa.llm.client import LLMResponse, TokenUsage
    from haa.llm.tools import ToolRegistry

    class FakeClient:
        def __init__(self):
            self.n = 0

        def call(self, msgs, *, campaign_id=None, stage=None):
            self.n += 1
            return LLMResponse(content=f"ans{self.n}", usage=TokenUsage(), model="fake")

    fc = FakeClient()
    reg = ToolRegistry(ToolsConfig(), campaigns_dir=tempfile.mkdtemp(), client=fc)
    out = reg.execute("multi_agents", {"tasks": [{"role": "angle1", "prompt": "a"}, {"prompt": "b"}]}, stage_name="SEEK", campaign_id="c1")
    assert "angle1" in out and "ans1" in out and "ans2" in out
    assert fc.n == 2  # both sub-agents ran


def test_multi_agents_no_client_errors():
    from haa.config import ToolsConfig
    from haa.llm.tools import ToolRegistry

    reg = ToolRegistry(ToolsConfig(), campaigns_dir="/tmp")  # no client
    with pytest.raises(ToolError):
        reg.execute("multi_agents", {"tasks": [{"prompt": "x"}]}, stage_name="SEEK")


# --- v0.9.1: calculator symbolic tolerant parsing --------------------------------

def test_calculator_symbolic_fenced_expression(registry):
    """```-fenced expressions parse (smoke failure mode: fenced prose)."""
    out = registry.execute("calculator", {
        "mode": "symbolic", "expression": "```\nx**2 + 2*x\n```",
    }, stage_name="DESIGN")
    assert "parsed:" in out and "x**2" in out.replace(" ", "")


def test_calculator_symbolic_prose_prefix(registry):
    """Leading prose line is dropped; the math-looking line is used."""
    out = registry.execute("calculator", {
        "mode": "symbolic",
        "expression": "We need to bound the advantage term:\nbeta*x + 1",
    }, stage_name="DESIGN")
    # Falls back to the last math line; parsed echo shows what was understood.
    assert "parsed:" in out


def test_calculator_symbolic_failure_names_format(registry):
    """Unparseable input → error includes a format example (teaches retry)."""
    with pytest.raises(ToolError) as exc_info:
        registry.execute("calculator", {
            "mode": "symbolic",
            "expression": "the advantage estimate for the unselected learner",
        }, stage_name="DESIGN")
    msg = str(exc_info.value)
    assert "simplify(x**2 + 2*x)" in msg
    assert "no prose" in msg


# --- calculator CPU watchdog (smoke4: sympy simplify hung the main thread 25min+) --
# v1.0.2：SIGALRM → fork 进程隔离（SIGALRM 仅主线程有效，API 服务器在工作
# 线程跑管线时护栏会静默失效；进程隔离在任何线程下行为一致）。

def test_calc_isolated_interrupts_runaway():
    """A stuck pure-Python compute is terminated at the timeout."""
    import time

    from haa.llm.tools import _CalcTimeout, _run_isolated

    with pytest.raises(_CalcTimeout):
        _run_isolated(lambda: (time.sleep(30), "never")[1], 0.5)


def test_calc_isolated_off_main_thread():
    """跨线程保证：工作线程内同样按时炸（这是弃用 SIGALRM 的原因）。"""
    import threading

    from haa.llm.tools import _CalcTimeout, _run_isolated

    outcome: list[Any] = []

    def _run():
        try:
            _run_isolated(lambda: "x" * 10**9, 0.5)
        except _CalcTimeout:
            outcome.append("timeout")
        except Exception as exc:  # noqa: BLE001
            outcome.append(f"other:{type(exc).__name__}")

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=15)
    assert outcome == ["timeout"]


def test_calc_child_error_surfaces():
    """Child-side exception text flows back to the parent."""
    from haa.llm.tools import _CalcChildError, _run_isolated

    def _boom():
        raise ZeroDivisionError("division by zero")

    with pytest.raises(_CalcChildError) as exc_info:
        _run_isolated(_boom, 5)
    assert "ZeroDivisionError" in str(exc_info.value)


def test_calculator_symbolic_timeout_raises_actionable_error(monkeypatch):
    """A pathological expression (child killed at timeout) → ToolError telling the model to split it."""
    from haa.llm.tools import _CalcTimeout

    def _explode(compute, seconds):
        raise _CalcTimeout("boom")

    monkeypatch.setattr(tools_mod, "_run_isolated", _explode)
    with pytest.raises(ToolError) as exc_info:
        tools_mod._calc_symbolic("simplify(x**2 + 2*x)")
    assert "timed out" in str(exc_info.value)
    assert "sub-expressions" in str(exc_info.value)


def test_calculator_numeric_timeout_raises_actionable_error(monkeypatch):
    from haa.llm.tools import _CalcTimeout

    def _explode(compute, seconds):
        raise _CalcTimeout("boom")

    monkeypatch.setattr(tools_mod, "_run_isolated", _explode)
    with pytest.raises(ToolError) as exc_info:
        tools_mod._calc_numeric("2**10 + 3*5")
    assert "timed out" in str(exc_info.value)


def test_calculator_happy_path_isolated(monkeypatch):
    """Cheap expression through the real fork path: correct result, no premature kill."""
    monkeypatch.setattr(tools_mod, "_CALC_TIMEOUT_S", 10.0)
    out = tools_mod._calc_symbolic("simplify(x**2 + 2*x)")
    assert "parsed:" in out and "x*(x" in out.replace(" ", "")


# --- exec_bash hardening (smoke4: model could pass timeout=99999; grandchildren survived) --

def test_exec_bash_timeout_clamped():
    assert tools_mod._clamp_exec_timeout(99999, 30) == 120
    assert tools_mod._clamp_exec_timeout("5", 30) == 5
    assert tools_mod._clamp_exec_timeout(None, 30) == 30
    assert tools_mod._clamp_exec_timeout("garbage", 30) == 30
    assert tools_mod._clamp_exec_timeout(0, 30) == 1


def test_exec_bash_model_timeout_above_ceiling_clamped(registry, tmp_path):
    """timeout=99999 is clamped to the 120s ceiling (fast command still runs fine)."""
    out = registry.execute("exec_bash", {
        "command": "echo clamped-ok", "timeout": 99999,
    }, stage_name="VERIFY")
    assert "clamped-ok" in out


def test_exec_bash_kills_background_grandchildren(registry):
    """A background grandchild ('sleep 300 &') dies with the timed-out group."""
    import subprocess as _sp

    with pytest.raises(ToolError) as exc_info:
        registry.execute("exec_bash", {
            "command": "sleep 300 & echo started; sleep 30", "timeout": 2,
        }, stage_name="VERIFY")
    assert "timed out" in str(exc_info.value)
    # 孙进程不得存活：扫一遍系统进程表找刚才那个 sleep 300。
    probe = _sp.run(
        ["pgrep", "-f", "sleep 300"], capture_output=True, text=True
    )
    assert probe.stdout.strip() == "", f"orphaned grandchildren: {probe.stdout}"


# --- v1.0.6-rev2: URL 安全校验 / grep / vision 接线 / OA 全文链接 ----------------


class TestUrlValidation:
    def test_web_fetch_rejects_localhost(self, registry):
        with pytest.raises(ToolSafetyError, match="internal hostname"):
            registry.execute("web_fetch", {"url": "http://localhost:8080/x"}, stage_name="SEEK")

    def test_web_fetch_rejects_private_ip(self, registry):
        with pytest.raises(ToolSafetyError, match="non-public"):
            registry.execute("web_fetch", {"url": "http://192.168.1.1/admin"}, stage_name="SEEK")

    def test_web_fetch_rejects_loopback_ip(self, registry):
        with pytest.raises(ToolSafetyError, match="non-public"):
            registry.execute("web_fetch", {"url": "http://127.0.0.1:8420/"}, stage_name="SEEK")

    def test_web_fetch_rejects_non_http_scheme(self, registry):
        with pytest.raises(ToolSafetyError, match="scheme"):
            registry.execute("web_fetch", {"url": "file:///etc/passwd"}, stage_name="SEEK")

    def test_web_fetch_allows_public_https(self, registry, monkeypatch):
        monkeypatch.setattr(tools_mod, "_http_get_text", lambda url, **kw: "ok")
        out = registry.execute("web_fetch", {"url": "https://example.com/a"}, stage_name="SEEK")
        assert out == "ok"

    def test_read_pdf_url_rejects_private(self, registry):
        with pytest.raises(ToolSafetyError):
            registry.execute("read_pdf", {"source": "http://10.0.0.1/p.pdf"}, stage_name="SEEK")


class TestGrepTool:
    def test_grep_finds_matches_with_line_numbers(self, registry, tmp_path):
        kdir = tmp_path / CAMP / "knowledge"
        kdir.mkdir(parents=True)
        (kdir / "notes.md").write_text("alpha portal\nbeta\nalpha gate\n", encoding="utf-8")
        out = registry.execute("grep", {"pattern": "alpha"}, stage_name="SEEK", campaign_id=CAMP)
        assert "knowledge/notes.md:1" in out and "knowledge/notes.md:3" in out
        assert "2 match" in out

    def test_grep_no_match(self, registry, tmp_path):
        (tmp_path / CAMP).mkdir(parents=True, exist_ok=True)
        (tmp_path / CAMP / "a.txt").write_text("nothing here", encoding="utf-8")
        out = registry.execute("grep", {"pattern": "zzz"}, stage_name="SEEK", campaign_id=CAMP)
        assert "no matches" in out

    def test_grep_rejects_bad_regex(self, registry):
        with pytest.raises(ToolError, match="invalid regex"):
            registry.execute("grep", {"pattern": "("}, stage_name="SEEK", campaign_id=CAMP)

    def test_grep_path_escape_blocked(self, registry):
        with pytest.raises(ToolSafetyError):
            registry.execute("grep", {"pattern": "x", "path": "../../etc"}, stage_name="SEEK", campaign_id=CAMP)


class TestVisionConfigWiring:
    def test_describe_image_uses_registry_vision_config(self, tmp_path, cfg, monkeypatch):
        """vision 段必须随 registry 走（v1.0.6-rev2 修复 default_config 硬编码）。"""
        from haa.llm import vision as vision_mod

        seen = {}

        class _FakeVision:
            model = "glm-5.3-flash"

        def fake_desc(**kwargs):
            seen["config"] = kwargs.get("config")
            return "desc"

        monkeypatch.setattr(vision_mod, "describe_image", fake_desc)
        vision_cfg = _FakeVision()
        reg = ToolRegistry(cfg, campaigns_dir=tmp_path, vision_config=vision_cfg)
        reg.execute("describe_image", {"image_url": "https://example.com/i.png"}, stage_name="SEEK")
        assert seen["config"] is vision_cfg

    def test_describe_image_falls_back_to_default_when_unset(self, tmp_path, cfg, monkeypatch):
        from haa.llm import vision as vision_mod

        seen = {}

        def fake_desc(**kwargs):
            seen["config"] = kwargs.get("config")
            return "desc"

        monkeypatch.setattr(vision_mod, "describe_image", fake_desc)
        reg = ToolRegistry(cfg, campaigns_dir=tmp_path)
        reg.execute("describe_image", {"image_url": "https://example.com/i.png"}, stage_name="SEEK")
        assert seen["config"] is not None
        assert getattr(seen["config"], "model", None)  # default_config().vision


class TestOaPdfLinks:
    def test_s2_results_include_open_access_pdf(self, registry, monkeypatch):
        monkeypatch.setattr(tools_mod, "_http_get_json", lambda url, **kw: {
            "data": [{
                "title": "Paper A", "year": 2025, "citationCount": 3,
                "externalIds": {"DOI": "10.1/x"},
                "openAccessPdf": {"url": "https://example.com/a.pdf"},
            }],
        })
        out = registry.execute("search_paper", {"query": "q"}, stage_name="SEEK")
        assert "PDF=https://example.com/a.pdf" in out

    def test_s2_no_pdf_field_omitted(self, registry, monkeypatch):
        monkeypatch.setattr(tools_mod, "_http_get_json", lambda url, **kw: {
            "data": [{"title": "Paper B", "year": 2024, "citationCount": 0,
                      "externalIds": {}}],
        })
        out = registry.execute("search_paper", {"query": "q"}, stage_name="SEEK")
        assert "PDF=" not in out

    def test_openalex_includes_best_oa_pdf(self, registry, monkeypatch):
        monkeypatch.setattr(tools_mod, "_http_get_json", lambda url, **kw: None)
        monkeypatch.setattr(
            tools_mod, "_search_openalex",
            lambda q, n, c: "- T (2025, cites=1) [DOI=] PDF=https://oa/x.pdf url",
        )
        out = registry.execute("search_paper", {"query": "q"}, stage_name="SEEK")
        assert "PDF=https://oa/x.pdf" in out
