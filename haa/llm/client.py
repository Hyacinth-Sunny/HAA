"""LLM client wrapper around ``litellm.completion``.

Responsibilities (HM-Pro Lessons 5 & 7):

* **Every call times out** (Lesson 7). ``timeout`` seconds per call; a timeout
  surfaces as :class:`LLMTimeoutError` so the caller can release the lane.
* **Transient failures retry with exponential backoff**, up to ``max_retries``
  extra attempts. A timeout is retryable; if every attempt times out the final
  error is :class:`LLMTimeoutError`, otherwise :class:`LLMRetryExhausted`.
* **Budget integration** (Lesson 5). Before the call we ``pre_spend`` an
  estimate (crash-safe: the deduction is in SQLite before the HTTP request);
  after the call we ``record`` the true cost from token usage. The gate follows
  the stage name — GRADE/WRITE are never blocked.
* **Structured output.** ``json_mode=True`` sends
  ``response_format={"type": "json_object"}``; :meth:`call_json` parses it.

The completion function and the backoff sleep are injectable so the client is
unit-testable without network access.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import litellm

from haa.budget import BudgetManager, Reservation, is_gated_stage

logger = logging.getLogger("haa.llm")


# --- 峰谷双档计价（2026-10-08 用户提供官方定义，北京时间） ---------------------


def _now_beijing():
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone(timedelta(hours=8)))


# 各家族高峰窗口（工作日=周一~周五；区间为 [start, end) 小时，北京时间）
_PEAK_WINDOWS = {
    "deepseek": ((9, 12), (14, 18)),   # 官方：工作日 9-12 与 14-18
    "glm": ((14, 18),),                # 官方：工作日 14-18
}


def _is_peak(model: str, now) -> bool:
    family = "glm" if "glm" in (model or "").lower() else "deepseek"
    if now.weekday() >= 5:  # 周末一律空闲
        return False
    hour = now.hour
    return any(start <= hour < end for start, end in _PEAK_WINDOWS[family])


def _tiered_rates(model: str, pricing: dict, now) -> tuple[float, float]:
    """按峰谷档取 (输入单价, 输出单价)——高峰用主键，空闲用 *_offpeak。"""
    if _is_peak(model, now):
        return float(pricing["input_per_m"]), float(pricing["output_per_m"])
    return (float(pricing.get("input_per_m_offpeak", pricing["input_per_m"])),
            float(pricing.get("output_per_m_offpeak", pricing["output_per_m"])))


# --- exceptions --------------------------------------------------------------


class LLMError(Exception):
    """Base class for client-raised errors."""


class LLMTimeoutError(LLMError):
    """The call (and all its retries) timed out.

    HM-Pro Lesson 7: the caller is responsible for releasing any held
    resources (lane, file handles) when this is raised.
    """


class LLMRetryExhausted(LLMError):
    """All retries were consumed on a non-timeout transient error."""


class LLMWallTimeoutError(LLMError):
    """Wall-clock ceiling exceeded mid-stream (smoke4 lesson).

    The idle gate (``timeout`` as a per-chunk read timeout under streaming)
    never fired — bytes were flowing — but the call has now generated longer
    than ``wall_timeout`` in total. Not retryable: a call that streamed past
    30 minutes is pathological, not transient.
    """


# --- response types ----------------------------------------------------------


@dataclass
class TokenUsage:
    """Token counts + derived USD cost for one completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class LLMResponse:
    """A normalised completion result."""

    content: str
    usage: TokenUsage
    model: str
    duration_s: float = 0.0
    attempts: int = 1
    raw: Any = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return bool(self.content)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls requested by the model (OpenAI function-calling format).

        Empty for plain ``call()``; populated when ``call_with_tools()`` is used
        and the model chose to call a tool. Each item is
        ``{"id", "type": "function", "function": {"name", "arguments": <json str>}}``.
        """
        try:
            calls = self.raw["choices"][0]["message"].get("tool_calls")
        except (KeyError, IndexError, TypeError, AttributeError):
            return []
        return calls or []


def _default_retryable() -> tuple[type[BaseException], ...]:
    """Transient litellm errors worth retrying. Built defensively (some may be
    absent on older/newer litellm versions)."""
    found = []
    for name in (
        "Timeout",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    ):
        exc = getattr(litellm, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            found.append(exc)
    return tuple(found)


RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = _default_retryable()


def _is_timeout(exc: BaseException) -> bool:
    """Heuristic: is this exception a timeout? Covers litellm.Timeout and any
    test double whose class name mentions 'timeout'."""
    if isinstance(exc, litellm.Timeout):
        return True
    return "timeout" in type(exc).__name__.lower()


def default_cost(completion_response: Any) -> float:
    """Best-effort USD cost via litellm's pricing map; 0.0 if unknown."""
    try:
        return float(litellm.completion_cost(completion_response=completion_response))
    except Exception:
        return 0.0


# --- client ------------------------------------------------------------------


class LLMClient:
    """Stateless-per-call wrapper around ``litellm.completion``.

    Parameters
    ----------
    model:
        litellm model string, e.g. ``"glm-5.2"`` or ``"openai/glm-4"``.
    timeout:
        Per-call timeout in seconds (Lesson 7). Default 300.
    max_retries:
        Extra attempts after the first on a transient failure. Default 2.
    api_base / api_key:
        Optional provider overrides (self-hosted / GLM endpoints).
    budget:
        Optional :class:`~haa.budget.BudgetManager`. When set *and* a
        ``campaign_id`` is passed to :meth:`call`, the spend protocol runs.
        completion_fn / cost_fn / sleep_fn:
            Injectable seams for testing. Defaults use litellm + ``time.sleep``.
        provider_options:
            Provider-specific request params sent verbatim via litellm's
            ``extra_body`` (GLM-5.3-flash needs ``tool_stream=True`` to emit
            tool_calls under streaming; also carries ``thinking`` /
            ``reasoning_effort``). A ``stream_options`` key here overrides the
            default include_usage as a top-level kwarg.
        pricing:
            Explicit USD-per-million-tokens fallback
            ``{"input_per_m": float, "output_per_m": float}``. Used when
            litellm's pricing map doesn't know the model (returns 0.0) —
            cost is then computed from real token counts so the budget
            gate stays sighted.
    """

    def __init__(
        self,
        model: str,
        *,
        timeout: float = 300,
        wall_timeout: float = 1800.0,
        max_retries: int = 2,
        api_base: str | None = None,
        api_key: str | None = None,
        budget: BudgetManager | None = None,
        retryable_exceptions: Sequence[type[BaseException]] | None = None,
        completion_fn: Callable[..., Any] | None = None,
        cost_fn: Callable[[Any], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        backoff_base: float = 1.0,
        provider_options: dict[str, Any] | None = None,
        pricing: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
        event_sink: Any = None,
    ):
        self.model = model
        self.timeout = float(timeout)
        # 双闸（smoke4 教训：litellm 的 timeout 是 httpx 读空闲超时，非流式下
        # 服务端持续生成会不断重置它 → 墙钟无界，实证 1041s 合法调用）。流式化
        # 后 timeout 变成"相邻 chunk 间隔上限"（长尾持续吐字节永不触发，真挂死
        # 300s 无字节即断）；wall_timeout 是第二道闸——chunk 循环内查总耗时。
        self.wall_timeout = float(wall_timeout)
        self.max_retries = max(0, int(max_retries))
        self.api_base = api_base or None
        self.api_key = api_key or None
        self.budget = budget
        self.retryable = tuple(retryable_exceptions) if retryable_exceptions else RETRYABLE_EXCEPTIONS
        self._completion_fn = completion_fn or litellm.completion
        self._cost_fn = cost_fn or default_cost
        self._sleep = sleep_fn or time.sleep
        self.backoff_base = float(backoff_base)
        self.provider_options = dict(provider_options or {})
        self.pricing = dict(pricing or {})
        self._log = logger or globals()["logger"]
        # Observability sink (Phase 6a-fronted). Local import to keep the
        # module-import graph acyclic (observability -> config, not -> client).
        from haa.observability import NullEventSink
        self.event_sink = event_sink if event_sink is not None else NullEventSink()

    # -- public API --------------------------------------------------------

    def call(
        self,
        messages: list[dict[str, str]],
        *,
        campaign_id: str | None = None,
        stage: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        estimate_cost: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Run one (retried) completion.

        Budget protocol: ``pre_spend`` before the call, ``record`` after. The
        gate mirrors the stage name (GRADE/WRITE are never blocked). On any
        failure path the reservation is refunded (the call produced nothing).
        """
        return self._retry_loop(
            messages,
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
            campaign_id=campaign_id,
            stage=stage,
            estimate_cost=estimate_cost,
        )

    def call_json(self, messages: list[dict[str, str]], **kwargs: Any) -> tuple[dict, LLMResponse]:
        """Like :meth:`call` with ``json_mode=True``, returning parsed JSON.

        Raises :class:`LLMError` if the content is not valid JSON.
        """
        response = self.call(messages, json_mode=True, **kwargs)
        try:
            parsed = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM did not return valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"LLM JSON was not an object: {type(parsed).__name__}")
        return parsed, response

    def call_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        *,
        campaign_id: str | None = None,
        stage: str | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        estimate_cost: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Like :meth:`call` but registers ``tools`` for function calling.

        The budget / retry / timeout semantics are identical to :meth:`call`: a
        crash-safe ``pre_spend`` before the request, retries on transient errors,
        and a refund on total failure. The difference is solely that ``tools``
        (OpenAI schema list) and an optional ``tool_choice`` are forwarded to the
        provider, and the returned :class:`LLMResponse` may carry ``tool_calls``
        (read via :attr:`LLMResponse.tool_calls`). ``call`` and ``call_json`` are
        untouched.
        """
        return self._retry_loop(
            messages,
            json_mode=False,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
            campaign_id=campaign_id,
            stage=stage,
            estimate_cost=estimate_cost,
            tools=tools,
            tool_choice=tool_choice,
        )

    def _retry_loop(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
        metadata: dict[str, Any] | None,
        campaign_id: str | None,
        stage: str | None,
        estimate_cost: float,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """Shared budget/retry/refund spine for :meth:`call` and
        :meth:`call_with_tools` (they were line-for-line duplicates except the
        tools kwargs; smoke4 流式化改造时合并，避免双份维护)."""
        should_gate = is_gated_stage(stage) if stage else True

        reservation: Reservation | None = None
        if self.budget is not None and campaign_id is not None:
            # Crash-safe: deduction is persisted before the HTTP request.
            reservation = self.budget.pre_spend(
                campaign_id, estimate_cost, gate=should_gate
            )

        start = time.monotonic()
        last_exc: BaseException | None = None
        last_was_timeout = False
        non_retryable: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            try:
                raw = self._invoke(
                    messages,
                    json_mode=json_mode,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    metadata=metadata,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                response = self._wrap(raw, attempt=attempt + 1, duration=time.monotonic() - start)
                self._log_call(stage, campaign_id, response, ok=True)
                if reservation is not None:
                    self.budget.record(reservation, response.usage.cost_usd)
                return response
            except self.retryable as exc:  # type: ignore[arg-type]
                last_exc = exc
                last_was_timeout = _is_timeout(exc)
                if attempt < self.max_retries:
                    backoff = self.backoff_base * (2 ** attempt)
                    self._log.info(
                        "llm transient %s (attempt %d/%d) — retrying in %.2fs: %s",
                        type(exc).__name__,
                        attempt + 1,
                        self.max_retries + 1,
                        backoff,
                        exc,
                    )
                    self._sleep(backoff)
                    continue
                break  # retries exhausted on a transient error
            except Exception as exc:  # non-retryable: stop immediately
                non_retryable = exc
                last_exc = exc
                break

        # --- failure path: refund, then raise the right error -----------
        duration = time.monotonic() - start
        if reservation is not None:
            # Call produced nothing usable → refund the estimate.
            self.budget.record(reservation, 0.0)
        self._log_call(stage, campaign_id, None, ok=False, duration=duration)

        if non_retryable is not None:
            raise LLMError(
                f"LLM call failed (non-retryable): {non_retryable}"
            ) from non_retryable
        if last_was_timeout:
            raise LLMTimeoutError(
                f"LLM call timed out after {self.timeout:g}s × {self.max_retries + 1} attempt(s)"
            ) from last_exc
        raise LLMRetryExhausted(
            f"LLM call failed after {self.max_retries + 1} attempt(s): {last_exc}"
        ) from last_exc

    # -- internals ---------------------------------------------------------

    def _invoke(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
        metadata: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": self.timeout,
            # 流式化：timeout 从"整调用墙钟"（会被持续生成重置、形同虚设）
            # 变为"相邻 chunk 间隔上限"——这正是区分长尾与挂死的判据。
            "stream": True,
            # 把重试权威收归 HAA 自己的 attempt 层（否则 litellm 内部再叠
            # 2 次重试，最坏 3×3 次调用）。
            "num_retries": 0,
            # 终 chunk 带 usage（成本/token 记账依赖它）。
            "stream_options": {"include_usage": True},
        }
        # Provider 专属参数（GLM 的 tool_stream/thinking 等）。litellm 的
        # openai 通道对未知顶层参数直接抛 UnsupportedParamsError（实测
        # 1.95.0），必须走官方 extra_body 通道逐字并入请求体；
        # stream_options 是 litellm 原生参数，单独走顶层覆盖默认。
        if self.provider_options:
            extra = {k: v for k, v in self.provider_options.items() if k != "stream_options"}
            if extra:
                kwargs["extra_body"] = extra
            if "stream_options" in self.provider_options:
                kwargs["stream_options"] = self.provider_options["stream_options"]
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if metadata:
            kwargs["metadata"] = metadata
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        resp = self._completion_fn(**kwargs)
        # 流式响应是迭代器；测试替身/非流式 provider 返回 dict——原样放行，
        # 下游 _wrap 两条路都能吃。
        if not isinstance(resp, dict) and hasattr(resp, "__next__"):
            prompt_chars = sum(
                len(str(m.get("content") or "")) for m in messages
            )
            return self._consume_stream(resp, prompt_chars=prompt_chars)
        return resp

    # -- streaming reassembly ----------------------------------------------

    @staticmethod
    def _field(obj: Any, key: str) -> Any:
        """Read ``key`` from a dict or an attribute-style object (litellm
        stream chunks come as ModelResponse/Delta objects in some versions,
        plain dicts in others)."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _consume_stream(self, stream: Any, *, prompt_chars: int = 0) -> dict[str, Any]:
        """Reassemble a token stream into a completion-shaped dict.

        Runs the wall-clock gate inside the chunk loop (the only place total
        elapsed is observable without a watchdog thread) and always closes the
        stream so a hanging connection cannot leak.
        """
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        model_name: str | None = None
        wall_deadline = time.monotonic() + self.wall_timeout
        try:
            for chunk in stream:
                if time.monotonic() > wall_deadline:
                    raise LLMWallTimeoutError(
                        f"LLM stream exceeded wall-clock ceiling "
                        f"{self.wall_timeout:g}s (idle gate never fired — "
                        f"generation was still producing)"
                    )
                c = chunk if isinstance(chunk, dict) else chunk.__dict__
                if self._field(c, "model"):
                    model_name = self._field(c, "model")
                u = self._field(c, "usage")
                if u:
                    usage = u if isinstance(u, dict) else u.__dict__
                choices = self._field(c, "choices") or []
                if not choices:
                    continue
                delta = (
                    choices[0].get("delta")
                    if isinstance(choices[0], dict)
                    else getattr(choices[0], "delta", None)
                ) or {}
                piece = self._field(delta, "content")
                if piece:
                    content_parts.append(piece)
                for tc in self._field(delta, "tool_calls") or []:
                    slot = tool_calls.setdefault(
                        int(self._field(tc, "index") or 0),
                        {"id": "", "type": "function",
                         "function": {"name": "", "arguments": ""}},
                    )
                    tc_id = self._field(tc, "id")
                    if tc_id:
                        slot["id"] = tc_id
                    fn = self._field(tc, "function") or {}
                    name = self._field(fn, "name")
                    if name:
                        slot["function"]["name"] = name
                    args = self._field(fn, "arguments")
                    if args:
                        slot["function"]["arguments"] += args
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — 关流失败不掩盖主结果
                    pass
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        # usage 双路径捕获（v1.0.5 修复 smoke5/6 记账失明）：末 chunk 之外，
        # litellm 的 CustomStreamWrapper 在迭代完成后会在 wrapper 对象上聚合
        # usage——两条路都取，仍空则按内容长度估算并打标（成本可观测优先）。
        usage = self._coalesce_usage(
            usage, stream, content_parts, tool_calls, prompt_chars=prompt_chars
        )
        return {
            "model": model_name or self.model,
            "choices": [{"message": message}],
            "usage": usage,
        }

    def _coalesce_usage(
        self,
        chunk_usage: dict[str, Any],
        stream: Any,
        content_parts: list[str],
        tool_calls: dict[int, dict[str, Any]],
        *,
        prompt_chars: int = 0,
    ) -> dict[str, Any]:
        """Merge usage from the final chunk, the stream wrapper, or estimate.

        smoke5/6 实证：deepseek 流式下末 chunk usage 可能为空且 provider 不总
        回传——预算闸因此失明两轮。顺序：末 chunk → wrapper 聚合 → 字符估算
        （估算结果打 ``estimated`` 标记，日志可辨）。
        """
        def _tokens_of(u: Any) -> tuple[int, int] | None:
            if not u:
                return None
            d = u if isinstance(u, dict) else getattr(u, "__dict__", {}) or {}
            try:
                p = int(d.get("prompt_tokens", 0) or 0)
                c = int(d.get("completion_tokens", 0) or 0)
            except (TypeError, ValueError):
                return None
            return (p, c) if (p or c) else None

        got = _tokens_of(chunk_usage)
        if got is None:
            # 流关闭后 wrapper 上可能已聚合（close 之后仍可读属性）
            try:
                got = _tokens_of(getattr(stream, "usage", None))
            except Exception:  # noqa: BLE001
                got = None
        if got is not None:
            out = {"prompt_tokens": got[0], "completion_tokens": got[1]}
            if got[0] == 0 and prompt_chars:
                # provider 只回了 completion 侧（deepseek 流式实证）——prompt
                # 按输入字符×1.3 补估并打标。
                out["prompt_tokens"] = int(prompt_chars * 1.3)
                out["estimated"] = True
            return out
        # 全空估算兜底：completion ≈ 输出字符×1.3；prompt ≈ 输入字符×1.3。
        comp_chars = sum(len(p) for p in content_parts) + sum(
            len(tc.get("function", {}).get("arguments") or "")
            + len(tc.get("function", {}).get("name") or "")
            for tc in tool_calls.values()
        )
        est_comp = int(comp_chars * 1.3)
        est_prompt = int(prompt_chars * 1.3)
        if est_comp or est_prompt:
            self._log.info(
                "llm usage estimated (stream returned none): prompt≈%d completion≈%d tokens",
                est_prompt, est_comp,
            )
            return {
                "prompt_tokens": est_prompt,
                "completion_tokens": est_comp,
                "estimated": True,
            }
        return {}

    def _wrap(self, raw: Any, *, attempt: int, duration: float) -> LLMResponse:
        content = ""
        try:
            content = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        usage = self._extract_usage(raw)
        return LLMResponse(
            content=content,
            usage=usage,
            model=self.model,
            duration_s=duration,
            attempts=attempt,
            raw=raw,
        )

    def _extract_usage(self, raw: Any) -> TokenUsage:
        prompt = completion = 0
        try:
            u = raw.get("usage") or {}
            prompt = int(u.get("prompt_tokens", 0) or 0)
            completion = int(u.get("completion_tokens", 0) or 0)
        except Exception:
            pass
        total = prompt + completion
        cost = self._cost_fn(raw)
        if not cost and self.pricing and (prompt or completion):
            # 价目兜底（遗留 #2 修）：litellm 价目表不认识的模型恒 0 →
            # 按配置单价从真实 token 数自算，预算闸不再致盲。
            # 峰谷双档（2026-10-08 用户提供的官方峰谷定义）：配置带
            # *_offpeak 时按北京时间判档取价，否则单档。
            try:
                in_rate = float(self.pricing["input_per_m"])
                out_rate = float(self.pricing["output_per_m"])
                if "input_per_m_offpeak" in self.pricing:
                    in_rate, out_rate = _tiered_rates(
                        self.model, self.pricing, _now_beijing())
                cost = (
                    prompt / 1_000_000 * in_rate
                    + completion / 1_000_000 * out_rate
                )
            except (KeyError, TypeError, ValueError):
                cost = 0.0
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cost_usd=cost,
        )

    def _log_call(
        self,
        stage: str | None,
        campaign_id: str | None,
        response: LLMResponse | None,
        *,
        ok: bool,
        duration: float = 0.0,
    ) -> None:
        if ok and response is not None:
            self._log.info(
                "llm ok | model=%s stage=%s campaign=%s "
                "tokens=%d(%d in/%d out) cost=$%.4f %.2fs attempt=%d",
                response.model,
                stage or "-",
                campaign_id or "-",
                response.usage.total_tokens,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.cost_usd,
                response.duration_s,
                response.attempts,
            )
            # Structured event: per-call cost/tokens/duration (Phase 6a-fronted).
            self.event_sink.emit(
                event_type="llm_call",
                campaign_id=campaign_id,
                stage=stage,
                cost_usd=response.usage.cost_usd,
                tokens=response.usage.total_tokens,
                duration_s=response.duration_s,
                payload={
                    "model": response.model,
                    "attempts": response.attempts,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                },
            )
        else:
            self._log.info(
                "llm FAIL | model=%s stage=%s campaign=%s %.2fs",
                self.model,
                stage or "-",
                campaign_id or "-",
                duration,
            )
            self.event_sink.emit(
                event_type="llm_call_failed",
                campaign_id=campaign_id,
                stage=stage,
                duration_s=duration,
                payload={"model": self.model},
            )
