"""Tests for the LLM client (mocked litellm — no network)."""

from __future__ import annotations

import json
from typing import Any

import litellm
import pytest

from haa.budget import BudgetManager
from haa.llm.client import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMRetryExhausted,
    LLMTimeoutError,
    TokenUsage,
)
from haa.models import Brief
from haa.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "haa.db")
    yield s
    s.close()


@pytest.fixture
def budget(store):
    return BudgetManager(store, global_limit=100.0)


@pytest.fixture
def campaign(store):
    return store.create_campaign(Brief(title="T", problem_area="P"), budget_limit=10.0)


def _resp(content: str = "hello", prompt: int = 10, completion: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


class _Transient(Exception):
    """A fake retryable error (not a timeout)."""


# --- success path -----------------------------------------------------------


def test_call_returns_content_and_usage(campaign):
    sleeps: list[float] = []
    client = LLMClient(
        "glm-5.2",
        completion_fn=lambda **kw: _resp("pong", 12, 8),
        cost_fn=lambda raw: 0.123,
        sleep_fn=sleeps.append,
    )
    resp = client.call([{"role": "user", "content": "hi"}])
    assert isinstance(resp, LLMResponse)
    assert resp.content == "pong"
    assert resp.usage.prompt_tokens == 12
    assert resp.usage.completion_tokens == 8
    assert resp.usage.total_tokens == 20
    assert resp.usage.cost_usd == pytest.approx(0.123)
    assert resp.attempts == 1
    assert sleeps == []  # no retries needed


def test_budget_pre_spend_then_record_true_up(budget, store, campaign):
    # cost_fn returns 0.4; we reserve 0.5 estimate; record true-ups to 0.4.
    client = LLMClient(
        "glm-5.2",
        budget=budget,
        completion_fn=lambda **kw: _resp("ok"),
        cost_fn=lambda raw: 0.4,
    )
    client.call(
        [{"role": "user", "content": "hi"}],
        campaign_id=campaign.id,
        stage="SEEK",
        estimate_cost=0.5,
    )
    fetched = store.get_campaign(campaign.id)
    assert fetched.budget_used == pytest.approx(0.4)  # true-up applied


def test_gate_follows_stage_name_gated(budget, store, campaign, monkeypatch):
    """SEEK is a gated stage → pre_spend called with gate=True."""
    seen = {}
    orig = budget.pre_spend

    def spy(cid, amt, *, gate=True):
        seen["gate"] = gate
        return orig(cid, amt, gate=gate)

    monkeypatch.setattr(budget, "pre_spend", spy)
    client = LLMClient(
        "glm-5.2",
        budget=budget,
        completion_fn=lambda **kw: _resp(),
        cost_fn=lambda raw: 0.0,
    )
    client.call([{"role": "user", "content": "x"}], campaign_id=campaign.id, stage="SEEK")
    assert seen["gate"] is True


def test_gate_follows_stage_name_ungated(budget, store, campaign, monkeypatch):
    """GRADE is ungated → pre_spend called with gate=False (Lesson 5)."""
    seen = {}
    orig = budget.pre_spend

    def spy(cid, amt, *, gate=True):
        seen["gate"] = gate
        return orig(cid, amt, gate=gate)

    monkeypatch.setattr(budget, "pre_spend", spy)
    client = LLMClient(
        "glm-5.2",
        budget=budget,
        completion_fn=lambda **kw: _resp(),
        cost_fn=lambda raw: 0.0,
    )
    client.call([{"role": "user", "content": "x"}], campaign_id=campaign.id, stage="GRADE")
    assert seen["gate"] is False


# --- json mode --------------------------------------------------------------


def test_json_mode_passes_response_format():
    captured: dict[str, Any] = {}

    def fake(**kw):
        captured.update(kw)
        return _resp(json.dumps({"verdict": "solid"}))

    client = LLMClient("glm-5.2", completion_fn=fake, cost_fn=lambda r: 0.0)
    parsed, resp = client.call_json([{"role": "user", "content": "grade it"}])
    assert captured["response_format"] == {"type": "json_object"}
    assert parsed == {"verdict": "solid"}
    assert resp.content == '{"verdict": "solid"}'


def test_json_mode_bad_json_raises():
    client = LLMClient(
        "glm-5.2",
        completion_fn=lambda **kw: _resp("not json at all"),
        cost_fn=lambda r: 0.0,
    )
    with pytest.raises(LLMError):
        client.call_json([{"role": "user", "content": "x"}])


def test_json_mode_non_object_raises():
    client = LLMClient(
        "glm-5.2",
        completion_fn=lambda **kw: _resp(json.dumps([1, 2, 3])),
        cost_fn=lambda r: 0.0,
    )
    with pytest.raises(LLMError):
        client.call_json([{"role": "user", "content": "x"}])


# --- retry / timeout / failure ----------------------------------------------


def test_retry_then_success():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient("transient blip")
        return _resp("finally", 3, 2)

    client = LLMClient(
        "glm-5.2",
        max_retries=2,
        completion_fn=flaky,
        cost_fn=lambda r: 0.0,
        sleep_fn=sleeps.append,
        retryable_exceptions=(_Transient,),
    )
    resp = client.call([{"role": "user", "content": "x"}])
    assert resp.content == "finally"
    assert resp.attempts == 3  # 1 initial + 2 retries
    assert len(sleeps) == 2
    # exponential backoff: base * 2^0, base * 2^1
    assert sleeps[0] == pytest.approx(1.0)
    assert sleeps[1] == pytest.approx(2.0)


def test_retry_exhausted_on_transient_non_timeout():
    sleeps: list[float] = []

    def always_fail(**kw):
        raise _Transient("nope")

    client = LLMClient(
        "glm-5.2",
        max_retries=2,
        completion_fn=always_fail,
        cost_fn=lambda r: 0.0,
        sleep_fn=sleeps.append,
        retryable_exceptions=(_Transient,),
    )
    with pytest.raises(LLMRetryExhausted):
        client.call([{"role": "user", "content": "x"}])
    assert len(sleeps) == 2  # slept between each retry


def test_timeout_raises_llm_timeout_error():
    sleeps: list[float] = []

    def hang(**kw):
        raise litellm.Timeout(
            model="glm-5.2", llm_provider="openai", message="read timed out"
        )

    client = LLMClient(
        "glm-5.2",
        timeout=1,
        max_retries=2,
        completion_fn=hang,
        cost_fn=lambda r: 0.0,
        sleep_fn=sleeps.append,
    )
    with pytest.raises(LLMTimeoutError):
        client.call([{"role": "user", "content": "x"}])
    assert len(sleeps) == 2


def test_non_retryable_raises_llm_error_immediately():
    sleeps: list[float] = []

    def bad(**kw):
        raise ValueError("bad request shape")

    client = LLMClient(
        "glm-5.2",
        max_retries=3,
        completion_fn=bad,
        cost_fn=lambda r: 0.0,
        sleep_fn=sleeps.append,
    )
    with pytest.raises(LLMError) as exc:
        client.call([{"role": "user", "content": "x"}])
    assert not isinstance(exc.value, (LLMTimeoutError, LLMRetryExhausted))
    assert sleeps == []  # no retries on a non-retryable error


# --- budget refund on failure ----------------------------------------------


def test_budget_refunded_on_failure(budget, store, campaign):
    def hang(**kw):
        raise litellm.Timeout(model="glm-5.2", llm_provider="openai", message="t/o")

    client = LLMClient(
        "glm-5.2",
        budget=budget,
        max_retries=1,
        completion_fn=hang,
        cost_fn=lambda r: 0.0,
    )
    with pytest.raises(LLMTimeoutError):
        client.call(
            [{"role": "user", "content": "x"}],
            campaign_id=campaign.id,
            stage="SEEK",
            estimate_cost=0.5,
        )
    # The estimate was reserved then refunded (record(0.0)) → net zero.
    assert store.get_campaign(campaign.id).budget_used == pytest.approx(0.0)


def test_no_budget_integration_when_budget_none():
    # Without a budget manager, call still works and never touches pre_spend.
    client = LLMClient(
        "glm-5.2",
        completion_fn=lambda **kw: _resp("ok"),
        cost_fn=lambda r: 0.0,
    )
    resp = client.call([{"role": "user", "content": "x"}])
    assert resp.content == "ok"


# --- streaming (smoke4 双闸改造：流式化 + chunk 重组 + 墙钟闸) ------------------

def _chunk(content=None, tool_calls=None, usage=None, model="deepseek-v4-pro"):
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    ch: dict[str, Any] = {"model": model, "choices": [{"delta": delta}]}
    if usage:
        ch["usage"] = usage
    return ch


def test_streaming_chunks_reassembled():
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([
            _chunk(content="Hel"),
            _chunk(content="lo "),
            _chunk(content="world"),
            _chunk(usage={"prompt_tokens": 7, "completion_tokens": 3}),
        ])

    client = LLMClient("deepseek/deepseek-v4-pro", completion_fn=fake_completion)
    resp = client.call([{"role": "user", "content": "hi"}])
    assert resp.content == "Hello world"
    assert resp.usage.prompt_tokens == 7 and resp.usage.completion_tokens == 3
    # 流式三件套必须透传给 provider
    assert captured["stream"] is True
    assert captured["num_retries"] == 0
    assert captured["stream_options"] == {"include_usage": True}
    assert captured["timeout"] == 300


def test_streaming_tool_calls_reassembled():
    def fake_completion(**kwargs):
        return iter([
            _chunk(tool_calls=[
                {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": ""}},
            ]),
            _chunk(tool_calls=[
                {"index": 0, "function": {"arguments": '{"q": "sub'}},
            ]),
            _chunk(tool_calls=[
                {"index": 0, "function": {"arguments": 'modular"}'}},
            ]),
            _chunk(usage={"prompt_tokens": 5, "completion_tokens": 2}),
        ])

    client = LLMClient("deepseek/deepseek-v4-pro", completion_fn=fake_completion)
    resp = client.call_with_tools([{"role": "user", "content": "go"}], [])
    calls = resp.tool_calls
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "search"
    assert calls[0]["function"]["arguments"] == '{"q": "submodular"}'


def test_stream_wall_timeout_is_fast_and_non_retryable():
    """无限吐字节的失控生成 → 墙钟闸起爆；不可重试（一次就炸，不烧 3×3）。"""
    import time as _time

    def fake_completion(**kwargs):
        def forever():
            while True:
                yield _chunk(content="x")
        return forever()

    client = LLMClient(
        "deepseek/deepseek-v4-pro", completion_fn=fake_completion,
        wall_timeout=0.05, sleep_fn=lambda s: None,
    )
    t0 = _time.monotonic()
    with pytest.raises(LLMError) as exc_info:
        client.call([{"role": "user", "content": "hi"}])
    assert _time.monotonic() - t0 < 5
    assert "wall-clock" in str(exc_info.value)


def test_call_with_tools_still_retries_after_merge():
    """合并 _retry_loop 后 call_with_tools 保留瞬态重试语义。"""
    calls = {"n": 0}

    def fake_completion(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Transient("boom")
        return _resp("recovered")

    client = LLMClient(
        "deepseek/deepseek-v4-pro", completion_fn=fake_completion,
        retryable_exceptions=(_Transient,), sleep_fn=lambda s: None,
    )
    resp = client.call_with_tools([{"role": "user", "content": "hi"}], None)
    assert resp.content == "recovered" and resp.attempts == 2


# --- v1.0.5 usage 双路径捕获（smoke5/6 记账失明修复） --------------------------

def test_stream_without_usage_falls_back_to_estimate():
    """chunk 无 usage → 按内容长度估算并打标（预算闸至少看得见 completion 侧）。"""

    def fake_completion(**kwargs):
        return iter([
            _chunk(content="A" * 2000),   # 无 usage chunk
            _chunk(content="B" * 1000),
        ])                                # 也没有终 chunk usage

    client = LLMClient("deepseek/deepseek-v4-pro", completion_fn=fake_completion)
    resp = client.call([{"role": "user", "content": "x" * 1000}])
    assert resp.usage.completion_tokens > 0, "估算兜底未生效"
    assert resp.usage.completion_tokens >= int(3000 * 1.3) - 5
    assert resp.usage.prompt_tokens >= int(1000 * 1.3) - 5  # 输入侧同估


def test_stream_wrapper_usage_picked_up_after_close():
    """wrapper 对象在迭代完成后聚合 usage → 优先于估算。"""

    class _Wrapper:
        """先迭代、close 后 usage 出现（模拟 litellm CustomStreamWrapper——
        注意必须有 __next__，否则 _invoke 的流式判定（hasattr __next__）不命中）。"""

        def __init__(self, chunks):
            self._chunks = list(chunks)
            self.usage = None

        def __iter__(self):
            return self

        def __next__(self):
            if self._chunks:
                return self._chunks.pop(0)
            raise StopIteration

        def close(self):
            self.usage = {"prompt_tokens": 11, "completion_tokens": 4}

    def fake_completion(**kwargs):
        return _Wrapper([_chunk(content="hello"), _chunk(content=" world")])

    client = LLMClient("deepseek/deepseek-v4-pro", completion_fn=fake_completion)
    resp = client.call([{"role": "user", "content": "hi"}])
    assert resp.usage.prompt_tokens == 11 and resp.usage.completion_tokens == 4


# --- provider_options（GLM-5.3-flash 适配）-----------------------------------


def test_provider_options_forwarded_to_completion_fn():
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk(content="ok")])

    client = LLMClient(
        "openai/glm-5.3-flash",
        completion_fn=fake_completion,
        provider_options={
            "tool_stream": True,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "medium",
        },
    )
    client.call([{"role": "user", "content": "hi"}])
    # GLM 流式工具调用硬需求：tool_stream 必须逐字进请求体（litellm 对未知
    # 顶层参数抛 UnsupportedParamsError，故走 extra_body 通道）
    assert captured["extra_body"] == {
        "tool_stream": True,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "medium",
    }
    # 默认三件套不受影响
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}


def test_provider_options_can_override_stream_options():
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk(content="ok")])

    client = LLMClient(
        "openai/glm-5.3-flash",
        completion_fn=fake_completion,
        provider_options={"tool_stream": True, "stream_options": None},
    )
    client.call([{"role": "user", "content": "hi"}])
    # provider 不认 include_usage 时可显式关掉（顶层覆盖，不进 extra_body）
    assert captured["stream_options"] is None
    assert captured["extra_body"] == {"tool_stream": True}


def test_provider_options_empty_by_default():
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk(content="ok")])

    client = LLMClient("deepseek/deepseek-v4-pro", completion_fn=fake_completion)
    client.call([{"role": "user", "content": "hi"}])
    assert "extra_body" not in captured
    assert "tool_stream" not in captured


# --- pricing 兜底（预算闸复明）------------------------------------------------


def test_pricing_fallback_when_litellm_cost_unknown():
    """litellm 价目表不认识的模型（cost=0）→ 按配置单价从真实 tokens 自算。"""

    def fake_completion(**kwargs):
        return iter([
            _chunk(content="ok", usage={"prompt_tokens": 1_000_000, "completion_tokens": 2_000_000}),
        ])

    client = LLMClient(
        "openai/glm-5.3-flash",
        completion_fn=fake_completion,
        cost_fn=lambda r: 0.0,  # litellm 无价目
        pricing={"input_per_m": 0.15, "output_per_m": 0.50},
    )
    resp = client.call([{"role": "user", "content": "hi"}])
    assert resp.usage.prompt_tokens == 1_000_000
    assert resp.usage.completion_tokens == 2_000_000
    # 1M×$0.15 + 2M×$0.50 = $1.15
    assert resp.usage.cost_usd == pytest.approx(1.15)


def test_pricing_not_used_when_litellm_has_cost():
    """litellm 已给出价（>0）时优先用 litellm，不叠加自算。"""

    def fake_completion(**kwargs):
        return iter([
            _chunk(content="ok", usage={"prompt_tokens": 1000, "completion_tokens": 1000}),
        ])

    client = LLMClient(
        "openai/glm-5.3-flash",
        completion_fn=fake_completion,
        cost_fn=lambda r: 0.002,
        pricing={"input_per_m": 0.15, "output_per_m": 0.50},
    )
    resp = client.call([{"role": "user", "content": "hi"}])
    assert resp.usage.cost_usd == pytest.approx(0.002)


def test_no_pricing_keeps_zero_cost():
    """未配价目且 litellm 无价 → 维持旧行为（0.0），不报错。"""

    def fake_completion(**kwargs):
        return iter([_chunk(content="ok", usage={"prompt_tokens": 5, "completion_tokens": 5})])

    client = LLMClient("openai/glm-5.3-flash", completion_fn=fake_completion, cost_fn=lambda r: 0.0)
    resp = client.call([{"role": "user", "content": "hi"}])
    assert resp.usage.cost_usd == 0.0
