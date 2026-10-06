"""Tests for the ReAct agent loop (haa/llm/agent_loop.py).

The LLM is a fake client that returns a programmed sequence of responses (some
with ``tool_calls``, then a final text answer). The tools are the real
:class:`~haa.llm.tools.ToolRegistry`, so tool execution is exercised end-to-end
with the file/bash sandbox — only the LLM is mocked.
"""

from __future__ import annotations

import json
import types

import pytest

from haa.config import load_config
from haa.llm.agent_loop import AgentLoop
from haa.llm.tools import ToolRegistry


# --- test doubles ------------------------------------------------------------


class FakeResp:
    """Stand-in for LLMResponse (only the fields the loop reads)."""

    def __init__(self, content="", tool_calls=None, cost=0.01):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = types.SimpleNamespace(cost_usd=cost)


class FakeClient:
    """Records calls and returns programmed responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call_with_tools(self, messages, tools, *, campaign_id=None, stage=None):
        self.calls.append({"messages": list(messages), "tools": tools, "stage": stage})
        if not self.responses:
            return FakeResp(content="(default final)")
        return self.responses.pop(0)


def tc(call_id: str, name: str, args: dict) -> dict:
    """Build one OpenAI-format tool_call."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def cfg():
    return load_config().tools


@pytest.fixture
def registry(tmp_path, cfg):
    return ToolRegistry(cfg, campaigns_dir=tmp_path, allowed_roots=[])


CAMP = "camp1"


# --- the loop ----------------------------------------------------------------

def test_tool_call_then_final_answer(registry):
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "echo 42"})]),
        FakeResp(content="the answer is 42"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("compute", stage_name="VERIFY", campaign_id=CAMP)

    assert result.content == "the answer is 42"
    assert result.iterations == 2
    assert not result.truncated
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "exec_bash"
    assert "42" in result.tool_calls[0].result
    assert result.tool_calls[0].error is None
    assert result.total_cost_usd == pytest.approx(0.02)  # two calls × 0.01
    # transcript carries the tool result back to the model
    assert any(m["role"] == "tool" and "42" in m["content"] for m in result.messages)
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in result.messages)


def test_immediate_final_answer_no_tools(registry):
    """max_tool_calls=0 → no tools offered → model answers directly."""
    client = FakeClient([FakeResp(content="direct answer")])
    loop = AgentLoop(client, registry)
    result = loop.run("hi", stage_name="VERIFY", campaign_id=CAMP, max_tool_calls=0)

    assert result.content == "direct answer"
    assert result.iterations == 1
    assert result.tool_calls == []
    # tools=None was forwarded (no tools offered)
    assert client.calls[0]["tools"] is None


def test_truncation_at_max_tool_calls(registry):
    """Each response asks for one tool; cap at 2 → final no-tools call."""
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "echo 1"})]),
        FakeResp(tool_calls=[tc("2", "exec_bash", {"command": "echo 2"})]),
        FakeResp(content="finalized with what I have"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("go", stage_name="VERIFY", campaign_id=CAMP, max_tool_calls=2)

    assert result.truncated is True
    assert result.content == "finalized with what I have"
    assert len(result.tool_calls) == 2
    # iter1 (tool) + iter2 (tool) + 1 final no-tools = 3 completions
    assert result.iterations == 3
    # the final call offered NO tools
    assert client.calls[-1]["tools"] is None
    # the budget-reached nudge was added to the transcript
    assert any(
        m["role"] == "user" and "tool-call budget reached" in m["content"]
        for m in client.calls[-1]["messages"]
    )


def test_tool_error_is_fed_back_not_raised(registry):
    """A failing tool must not crash the loop; its error goes back to the model."""
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "read_file", {"path": "missing.txt"})]),
        FakeResp(content="recovered after the tool failed"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("read", stage_name="VERIFY", campaign_id=CAMP)

    assert result.content == "recovered after the tool failed"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].error is not None
    assert "ToolError" in result.tool_calls[0].error
    # the error text was the tool message content
    assert any(m["role"] == "tool" and "ToolError" in m["content"] for m in result.messages)


def test_stage_filters_offered_tools(registry):
    """The loop must only offer the tools the stage is allowed to use."""
    client = FakeClient([FakeResp(content="ok")])
    loop = AgentLoop(client, registry)
    loop.run("search", stage_name="SEEK", campaign_id=CAMP)

    offered = sorted(t["function"]["name"] for t in client.calls[0]["tools"])
    # SEEK allows fetch_paper_fulltext + describe_image + grep + multi_agents + read_file + read_pdf + three web/search tools
    assert offered == ["describe_image", "fetch_paper_fulltext", "grep", "multi_agents", "read_file", "read_pdf", "search_paper", "web_fetch", "web_search"]
    assert "exec_bash" not in offered
    assert "write_file" not in offered


def test_stage_and_campaign_forwarded(registry):
    client = FakeClient([FakeResp(content="ok")])
    loop = AgentLoop(client, registry)
    loop.run("x", stage_name="DESIGN", campaign_id=CAMP)

    assert client.calls[0]["stage"] == "DESIGN"


def test_json_mode_adds_instruction(registry):
    client = FakeClient([FakeResp(content='{"k": 1}')])
    loop = AgentLoop(client, registry)
    result = loop.run("emit json", stage_name="VERIFY", campaign_id=CAMP, json_mode=True)

    system_msgs = [m for m in client.calls[0]["messages"] if m["role"] == "system"]
    assert system_msgs and "JSON" in system_msgs[0]["content"]
    assert result.content == '{"k": 1}'


def test_no_registry_offers_no_tools(registry):
    """AgentLoop(client, None) must not offer or execute any tool."""
    client = FakeClient([FakeResp(content="answer")])
    loop = AgentLoop(client, None)
    result = loop.run("hi", stage_name="SEEK")
    assert client.calls[0]["tools"] is None
    assert result.tool_calls == []
    assert result.content == "answer"


def test_multi_tool_batch_executes_all(registry):
    """Two tool_calls in one assistant turn both get responses (API integrity)."""
    client = FakeClient([
        FakeResp(tool_calls=[
            tc("1", "exec_bash", {"command": "echo a"}),
            tc("2", "exec_bash", {"command": "echo b"}),
        ]),
        FakeResp(content="both done"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("run both", stage_name="VERIFY", campaign_id=CAMP, max_tool_calls=10)

    assert result.content == "both done"
    assert len(result.tool_calls) == 2
    # both tool_call_ids have a matching tool message
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"1", "2"}


# --- v1.0.2: conversation-side truncation + stage deadline --------------------

def test_tool_result_truncated_in_conversation(registry):
    """20K exec_bash output → conversation copy capped at 8K with a visible
    marker (smoke4: full-fidelity accumulation peaked DESIGN at 51.7K in)."""
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "printf 'A%.0s' $(seq 1 20000)"})]),
        FakeResp(content="done"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("big", stage_name="VERIFY", campaign_id=CAMP)

    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert len(content) < 9000
    assert "chars truncated" in content


def test_read_file_gets_bigger_cap(registry, tmp_path):
    """read_file 的 30K 独立帽：20K 知识文件整读无截断，45K 才截。"""
    camp = tmp_path / CAMP
    camp.mkdir(parents=True, exist_ok=True)
    (camp / "k20.txt").write_text("K" * 20000, encoding="utf-8")
    (camp / "k45.txt").write_text("L" * 45000, encoding="utf-8")
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "read_file", {"path": "k20.txt"})]),
        FakeResp(tool_calls=[tc("2", "read_file", {"path": "k45.txt"})]),
        FakeResp(content="done"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("knowledge", stage_name="SEEK", campaign_id=CAMP)

    reads = [m["content"] for m in result.messages
             if m.get("role") == "tool" and m["content"].startswith("K")]
    assert len(reads) == 1 and len(reads[0]) == 20000  # 整读
    big = [m["content"] for m in result.messages
           if m.get("role") == "tool" and m["content"].startswith("L")]
    assert len(big) == 1 and len(big[0]) < 31000 and "chars truncated" in big[0]


def test_stage_deadline_forces_final_answer(registry):
    """deadline 到点 → 与工具帽同款收尾（无工具终答，truncated=True）。"""
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "sleep 0.4"})]),
        FakeResp(content="final under deadline"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("deadline", stage_name="VERIFY", campaign_id=CAMP,
                      deadline_s=0.1)  # 工具耗时 0.4s > 死线 0.1s——确定性触发

    assert result.truncated
    assert result.content == "final under deadline"
    assert any(
        "stage wall-clock deadline reached" in str(m.get("content", ""))
        for m in result.messages
    )


def test_stage_deadline_not_hit_when_generous(registry):
    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "echo ok"})]),
        FakeResp(content="done"),
    ])
    loop = AgentLoop(client, registry)
    result = loop.run("fine", stage_name="VERIFY", campaign_id=CAMP,
                      deadline_s=600)
    assert not result.truncated


def test_tool_call_event_emitted_to_sink(registry):
    """v1.0.6-rev2：每个 tool_call 落事件流（调参数据源）。"""
    from haa.observability import SQLiteEventSink

    # 借真实 StateStore 太重——用一个记录型 sink 验证调用契约即可
    emitted = []

    class RecordingSink:
        def emit(self, **kwargs):
            emitted.append(kwargs)

    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "echo 7"})]),
        FakeResp(content="done"),
    ])
    loop = AgentLoop(client, registry, event_sink=RecordingSink())
    loop.run("go", stage_name="VERIFY", campaign_id=CAMP)

    tool_events = [e for e in emitted if e["event_type"] == "tool_call"]
    assert len(tool_events) == 1
    e = tool_events[0]
    assert e["stage"] == "VERIFY" and e["campaign_id"] == CAMP
    assert e["payload"]["tool"] == "exec_bash"
    assert e["payload"]["ok"] is True
    assert e["duration_s"] >= 0

def test_tool_call_event_records_failure(registry):
    emitted = []

    class RecordingSink:
        def emit(self, **kwargs):
            emitted.append(kwargs)

    client = FakeClient([
        FakeResp(tool_calls=[tc("1", "exec_bash", {"command": "rm -rf /"})]),
        FakeResp(content="noted"),
    ])
    loop = AgentLoop(client, registry, event_sink=RecordingSink())
    loop.run("go", stage_name="VERIFY", campaign_id=CAMP)

    tool_events = [e for e in emitted if e["event_type"] == "tool_call"]
    assert tool_events[0]["payload"]["ok"] is False
    assert tool_events[0]["payload"]["error"]
