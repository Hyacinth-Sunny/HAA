"""大修批次1（M0）——新 Harness 核心五件套 + 换壳桥接 + D2 门卫的测试。

覆盖：registry 四件套与菜单覆盖语义、session_log 双名记账与冻结、
checklist 六步链顺序与写闸门、prompt_sections 楼层拼装与逃生舱、
agent_loop 迁移后的行为保持、legacy 桥接的 16 工具等价性、
tool_menu.yaml 快照与代码声明一致（回归锚定）、ACP 停用门卫。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from haa.harness import registry as hr
from haa.harness.agent_loop import AgentLoop
from haa.harness.checklist import (
    Checklist,
    PathWhitelist,
    ReadBeforeWriteGate,
    ReadTracker,
)
from haa.harness.prompt_sections import PromptSectionRegistry
from haa.harness.registry import (
    ToolMenu,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    tool,
)
from haa.harness.session_log import SessionEventLog, _RecordingSink
from haa.harness.tools_bridge import registry_from_legacy


# --------------------------------------------------------------------------- #
#  测试桩
# --------------------------------------------------------------------------- #

def _spec(name="t_echo", stages=("SEEK", "NOVELTY"), guardrails=None, handler=None):
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        stages=stages,
        handler=handler or (lambda args, ctx: ToolResult(content=str(args.get("x", "")))),
        guardrails=guardrails or {},
        source="test",
    )


class _Resp:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = SimpleNamespace(cost_usd=0.0)


class _ScriptedClient:
    """按脚本依次返回 tool_call / 最终文本；记录收到的 messages。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[dict] = []

    def call_with_tools(self, messages, tools, *, campaign_id=None, stage=None):
        self.seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self.script.pop(0)


def _tc(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# --------------------------------------------------------------------------- #
#  registry：四件套 + 菜单
# --------------------------------------------------------------------------- #

class TestRegistry:

    def test_decorator_attaches_spec_and_registers(self):
        reg = ToolRegistry()

        @tool(name="glob", description="find files",
              parameters={"type": "object", "properties": {}, "required": []},
              stages=("SEEK",))
        def glob_(args, ctx):
            return ToolResult(content="ok")

        reg.register_spec(glob_)
        assert reg.names() == ["glob"]
        assert reg.invoke("glob", {}, stage_name="SEEK").content == "ok"

    def test_duplicate_name_rejected(self):
        reg = ToolRegistry()
        reg.register(_spec())
        with pytest.raises(ValueError, match="duplicate"):
            reg.register(_spec())

    def test_stage_filter_and_menu_override(self):
        reg = ToolRegistry()
        reg.register(_spec(stages=("SEEK",)))
        assert reg.get_schemas("SEEK")[0]["function"]["name"] == "t_echo"
        assert reg.get_schemas("NOVELTY") == []
        # 菜单按工具覆盖：文件声明 NOVELTY → 覆盖代码声明
        reg.menu = ToolMenu({"t_echo": ["NOVELTY"]})
        assert reg.get_schemas("SEEK") == []
        assert reg.get_schemas("NOVELTY")[0]["function"]["name"] == "t_echo"

    def test_menu_does_not_touch_unlisted_tools(self):
        reg = ToolRegistry()
        reg.register(_spec(name="a", stages=("SEEK",)))
        reg.register(_spec(name="b", stages=("SEEK",)))
        reg.menu = ToolMenu({"a": ["NOVELTY"]})  # b 未登记 → 沿用代码声明
        names = [s["function"]["name"] for s in reg.get_schemas("SEEK")]
        assert names == ["b"]

    def test_empty_stage_allows_all(self):
        # 旧语义：无阶段上下文不拦（测试/临时调用）
        reg = ToolRegistry()
        reg.register(_spec(stages=("SEEK",)))
        assert reg.get_schemas("") != []
        reg.invoke("t_echo", {"x": "1"})  # 不抛权限错

    def test_unknown_and_forbidden_raise(self):
        reg = ToolRegistry()
        reg.register(_spec(stages=("SEEK",)))
        with pytest.raises(hr.ToolError, match="unknown tool"):
            reg.invoke("nope", {})
        with pytest.raises(hr.ToolError, match="not allowed"):
            reg.invoke("t_echo", {"x": "1"}, stage_name="GRADE")

    def test_execute_alias_compat(self):
        reg = ToolRegistry()
        reg.register(_spec())
        # 兼容别名与旧签名同形
        assert reg.execute("t_echo", {"x": "z"}, stage_name="SEEK").content == "z"


# --------------------------------------------------------------------------- #
#  session_log：双名记账 + 冻结
# --------------------------------------------------------------------------- #

class TestSessionLog:

    def test_tool_call_and_result_events_with_legacy_mirror(self):
        sink = _RecordingSink()
        log = SessionEventLog(sink)
        seq = log.tool_call(tool="read_file", arguments={"path": "a.md"},
                            stage="SEEK", campaign_id="c1")
        ok = log.tool_result(tool="read_file", call_seq=seq, ok=True,
                             result_head="hello", duration_s=0.1,
                             stage="SEEK", campaign_id="c1")
        assert ok
        types = [e["event_type"] for e in sink.events]
        # 新名（tool/call、tool/result）+ 旧名镜像（tool_call，tool-stats 兼容）
        assert types == ["tool/call", "tool_call", "tool/result", "tool_call"]
        payload0 = sink.events[0]["payload"]
        assert payload0["tool"] == "read_file" and payload0["arguments"] == {"path": "a.md"}
        # call 侧镜像只带 tool/arguments（不含 ok——那是 result 侧字段）
        mirror = sink.events[1]["payload"]
        assert mirror["tool"] == "read_file" and "call_seq" not in mirror
        result_mirror = sink.events[3]["payload"]
        assert result_mirror["ok"] is True and "call_seq" not in result_mirror

    def test_result_frozen_once(self):
        sink = _RecordingSink()
        log = SessionEventLog(sink)
        log.tool_call(tool="t", arguments={}, stage="S")
        assert log.tool_result(tool="t", call_seq=1, ok=True) is True
        assert log.tool_result(tool="t", call_seq=1, ok=False, error="x") is False
        # 冻结失败不追加事件
        result_events = [e for e in sink.events if e["event_type"] == "tool/result"]
        assert len(result_events) == 1


# --------------------------------------------------------------------------- #
#  checklist：六步链
# --------------------------------------------------------------------------- #

class TestChecklist:

    def _reg_with_sink(self, *, gate=False, roots=(), prechecks=None):
        sink = _RecordingSink()
        reg = ToolRegistry(checklist=Checklist(
            session_log=SessionEventLog(sink),
            prechecks=prechecks or [PathWhitelist(allowed_roots=roots)],
            write_gate=ReadBeforeWriteGate(enabled=gate),
        ))
        return reg, sink

    def test_six_step_order_call_before_handler_result_after(self):
        order: list[str] = []

        def handler(args, ctx):
            order.append("handler")
            return ToolResult(content="done")

        reg, sink = self._reg_with_sink()
        reg.register(ToolSpec(
            name="t", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=handler, source="test",
        ))
        out = reg.invoke("t", {"x": 1}, stage_name="SEEK")
        assert out.content == "done"
        assert order == ["handler"]
        types = [e["event_type"] for e in sink.events]
        # 先记账（tool/call）→ 执行 → 冻结记账（tool/result）
        assert types[0] == "tool/call" and "handler" in order and types[-2] == "tool/result"
        assert sink.events[0]["payload"]["tool"] == "t"

    def test_precheck_rejection_is_accounted_and_raised(self):
        called = {"n": 0}

        def handler(args, ctx):
            called["n"] += 1
            return "never"

        tmp = Path("/definitely/not/allowed")
        reg, sink = self._reg_with_sink(roots=(tmp,))
        reg.register(ToolSpec(
            name="t", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=handler, guardrails={"reads_paths": True}, source="test",
        ))
        with pytest.raises(hr.ToolError, match="outside allowed roots"):
            reg.invoke("t", {"path": "/etc/passwd"}, stage_name="SEEK")
        assert called["n"] == 0  # 检查拒绝了，执行体没跑
        result_events = [e for e in sink.events if e["event_type"] == "tool/result"]
        assert result_events[0]["payload"]["ok"] is False

    def test_handler_error_fed_back_as_toolerror_with_accounting(self):
        def boom(args, ctx):
            raise RuntimeError("kaboom")

        reg, sink = self._reg_with_sink()
        reg.register(ToolSpec(
            name="t", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=boom, source="test",
        ))
        with pytest.raises(hr.ToolError, match="RuntimeError: kaboom"):
            reg.invoke("t", {}, stage_name="SEEK")
        result_events = [e for e in sink.events if e["event_type"] == "tool/result"]
        assert result_events[0]["payload"]["ok"] is False

    def test_write_gate_blocks_unread_and_mtime_changed(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("v1", encoding="utf-8")

        def noop(args, ctx):
            return "ok"

        reg, _ = self._reg_with_sink(gate=True, roots=(tmp_path,))
        reg.register(ToolSpec(
            name="reader", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=noop, guardrails={"reads_paths": True}, source="test",
        ))
        reg.register(ToolSpec(
            name="writer", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=noop, guardrails={"writes_paths": True}, source="test",
        ))
        # 未读先写 → 拒
        with pytest.raises(hr.ToolError, match="never read"):
            reg.invoke("writer", {"path": str(f)}, stage_name="SEEK")
        # 读 → 写（同 mtime）→ 过
        reg.invoke("reader", {"path": str(f)}, stage_name="SEEK")
        assert reg.invoke("writer", {"path": str(f)}, stage_name="SEEK").content == "ok"
        # mtime 变 → 拒
        future = time.time() + 50
        os.utime(f, (future, future))
        with pytest.raises(hr.ToolError, match="mtime"):
            reg.invoke("writer", {"path": str(f)}, stage_name="SEEK")

    def test_write_gate_off_by_default(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("v1", encoding="utf-8")

        reg, _ = self._reg_with_sink(gate=False, roots=(tmp_path,))
        reg.register(ToolSpec(
            name="writer", description="", parameters={"type": "object", "properties": {}},
            stages="*", handler=lambda a, c: "ok",
            guardrails={"writes_paths": True}, source="test",
        ))
        assert reg.invoke("writer", {"path": str(f)}, stage_name="SEEK").content == "ok"

    def test_chain_overhead_within_budget(self):
        # §6.4 性能预算：检查层+记账（不含工具本体）≤ 50ms 量级
        sink = _RecordingSink()
        reg = ToolRegistry(checklist=Checklist(session_log=SessionEventLog(sink)))
        reg.register(_spec())
        t0 = time.monotonic()
        for i in range(100):
            reg.invoke("t_echo", {"x": str(i)}, stage_name="SEEK")
        per_call_ms = (time.monotonic() - t0) * 1000 / 100
        assert per_call_ms < 50


# --------------------------------------------------------------------------- #
#  prompt_sections：楼层
# --------------------------------------------------------------------------- #

class TestPromptSections:

    def test_floor_ordering_and_same_floor_registration_order(self):
        r = PromptSectionRegistry()
        r.register(source="stage", floor=200, text="STAGE")
        r.register(source="identity", floor=-100, text="IDENTITY")
        r.register(source="b", floor=100, text="TOOL-B")
        r.register(source="a", floor=100, text="TOOL-A")
        assert r.assemble() == "IDENTITY\n\nTOOL-B\n\nTOOL-A\n\nSTAGE"

    def test_dynamic_render_and_blank_sections_skipped(self):
        r = PromptSectionRegistry()
        r.register(source="dyn", floor=0, render=lambda ctx: "brief for " + ctx.get("stage", "?"))
        r.register(source="empty", floor=50, text="   ")
        assert r.assemble({"stage": "SEEK"}) == "brief for SEEK"

    def test_complete_escape_hatch(self):
        r = PromptSectionRegistry()
        r.register(source="identity", floor=-100, text="IDENTITY")
        r.register(source="subagent", floor=200, text="FULL PERSONA", complete=True)
        assert r.assemble() == "FULL PERSONA"


# --------------------------------------------------------------------------- #
#  agent_loop：迁移后的行为保持（对照 tests/test_agent_loop.py 的旧循环用例）
# --------------------------------------------------------------------------- #

class TestHarnessAgentLoop:

    def _loop(self, client, reg=None, **kw):
        return AgentLoop(client, reg if reg is not None else ToolRegistry(), **kw)

    def test_single_tool_call_then_final_answer(self):
        reg = ToolRegistry()
        reg.register(_spec())
        client = _ScriptedClient([
            _Resp(tool_calls=[_tc("t_echo", {"x": "hi"})]),
            _Resp(content="final"),
        ])
        result = self._loop(client, reg).run("go", stage_name="SEEK")
        assert result.content == "final"
        assert result.tool_calls[0].result == "hi"
        assert result.tool_calls[0].error is None
        # 消息配对完整：assistant(tool_calls) 后跟 tool 应答
        roles = [(m["role"], m.get("tool_call_id")) for m in result.messages]
        assert ("tool", "c1") in roles

    def test_tool_error_fed_back_not_crash(self):
        reg = ToolRegistry()
        reg.register(_spec(stages=("GRADE",)))  # SEEK 不允许 → 权限错
        client = _ScriptedClient([
            _Resp(tool_calls=[_tc("t_echo", {"x": "1"})]),
            _Resp(content="recovered"),
        ])
        result = self._loop(client, reg).run("go", stage_name="SEEK")
        assert result.content == "recovered"
        assert result.tool_calls[0].error and "not allowed" in result.tool_calls[0].error

    def test_cap_triggers_no_tool_finalize(self):
        reg = ToolRegistry()
        reg.register(_spec())
        client = _ScriptedClient([
            _Resp(tool_calls=[_tc("t_echo", {"x": "1"})]),
            _Resp(tool_calls=[_tc("t_echo", {"x": "2"})]),
            _Resp(content="wrapped up"),
        ])
        result = self._loop(client, reg).run("go", stage_name="SEEK", max_tool_calls=2)
        assert result.truncated is True
        assert result.content == "wrapped up"
        # 收尾那次调用不带工具菜单
        assert client.seen[-1]["tools"] is None

    def test_tool_result_events_emitted_via_checklist(self):
        sink = _RecordingSink()
        reg = ToolRegistry()
        reg.register(_spec())
        loop = AgentLoop(_ScriptedClient([
            _Resp(tool_calls=[_tc("t_echo", {"x": "1"})]),
            _Resp(content="done"),
        ]), reg, event_sink=sink)
        loop.run("go", stage_name="SEEK")
        types = [e["event_type"] for e in sink.events]
        assert "tool/call" in types and "tool/result" in types and "tool_call" in types

    def test_sections_assembly_when_system_prompt_none(self):
        r = PromptSectionRegistry()
        r.register(source="identity", floor=-100, text="IDENT")
        client = _ScriptedClient([_Resp(content="ok")])
        self._loop(client, sections=r).run("go")
        sys_msg = client.seen[0]["messages"][0]
        assert sys_msg["role"] == "system" and sys_msg["content"] == "IDENT"


# --------------------------------------------------------------------------- #
#  legacy 桥接：16 工具换壳等价性
# --------------------------------------------------------------------------- #

class TestLegacyBridge:

    def test_all_16_tools_reshelled(self):
        from haa.llm.tools import ToolRegistry as LegacyRegistry
        legacy = LegacyRegistry()
        reg = registry_from_legacy(legacy)
        assert reg.names() == legacy.names()
        assert len(reg.names()) == 16

    @pytest.mark.parametrize("stage", [
        "SEEK", "NOVELTY", "SCREEN", "DESIGN", "VERIFY", "GRADE",
        "WRITE", "REVIEW", "REFINE", "EXP_SPEC", "EXP_FEASIBILITY", "",
    ])
    def test_stage_menus_equal_legacy(self, stage):
        from haa.llm.tools import ToolRegistry as LegacyRegistry
        legacy = LegacyRegistry()
        reg = registry_from_legacy(legacy)
        # 顺序口径不同（新表按名排序、旧表按登记序），集合等价即行为等价
        assert (
            sorted(s["function"]["name"] for s in reg.get_schemas(stage))
            == sorted(s["function"]["name"] for s in legacy.get_schemas(stage))
        )

    def test_bridge_invoke_read_file_in_sandbox(self, tmp_path):
        from haa.llm.tools import ToolRegistry as LegacyRegistry
        (tmp_path / "knowledge").mkdir()
        (tmp_path / "knowledge" / "a.md").write_text("内容 ABC", encoding="utf-8")
        legacy = LegacyRegistry(campaigns_dir=tmp_path)
        reg = registry_from_legacy(legacy)
        out = reg.invoke("read_file", {"path": "knowledge/a.md"}, stage_name="SEEK")
        assert "内容 ABC" in out.content

    def test_tool_menu_yaml_snapshot_matches_code(self):
        # 回归锚定：M0 生成的声明式菜单与旧注册表代码声明逐一等价
        from haa.llm.tools import ToolRegistry as LegacyRegistry
        legacy = LegacyRegistry()
        menu = ToolMenu.load("config/tool_menu.yaml")
        for name in legacy.names():
            t = legacy.get(name)
            declared = set(t.allowed_stages)
            if "*" in declared:
                assert menu.stage_set(name) is None, f"{name} 全阶段工具不应进菜单文件"
                continue
            assert menu.stage_set(name) == declared, f"{name} 菜单与代码声明漂移"


# --------------------------------------------------------------------------- #
#  config + D2 门卫
# --------------------------------------------------------------------------- #

class TestConfigAndACPGate:

    def test_default_yaml_parses_harness_and_acp(self):
        from haa.config import default_config
        cfg = default_config()
        assert cfg.acp.enabled is False  # D2 停用保留默认关
        assert cfg.harness.tool_menu == "config/tool_menu.yaml"
        assert cfg.harness.write_gate_enabled is False
        assert cfg.harness.feature("anchored_mode") is False
        assert cfg.harness.feature("settlement") is False

    def test_build_coding_agent_blocked_when_disabled(self, tmp_path):
        from haa.config import Config, StorageConfig
        from haa.project_controller import ProjectController
        cfg = Config(storage=StorageConfig(
            db_path=str(tmp_path / "t.db"), campaigns_dir=str(tmp_path / "camps")))
        ctrl = ProjectController.__new__(ProjectController)
        ctrl.config = cfg
        ctrl._coding_agent_factory = None
        with pytest.raises(RuntimeError, match="已停用"):
            ctrl._build_coding_agent(
                SimpleNamespace(id="p1", selected_precursor_campaign_id=None), tmp_path
            )

    def test_build_coding_agent_allowed_when_enabled(self, tmp_path):
        from haa.config import ACPConfig, Config, StorageConfig
        from haa.project_controller import ProjectController
        cfg = Config(
            storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                  campaigns_dir=str(tmp_path / "camps")),
            acp=ACPConfig(enabled=True),
        )
        ctrl = ProjectController.__new__(ProjectController)
        ctrl.config = cfg
        ctrl._coding_agent_factory = None
        agent = ctrl._build_coding_agent(
            SimpleNamespace(id="p1", selected_precursor_campaign_id=None), tmp_path / "wd"
        )
        assert agent.session_name.startswith("haa-")


# --------------------------------------------------------------------------- #
#  BaseStage 集成：_make_agent_loop 返回新 Harness 循环
# --------------------------------------------------------------------------- #

class TestBaseStageIntegration:

    def test_make_agent_loop_builds_harness_loop(self, monkeypatch):
        from haa.stages.seek import SeekStage

        stage = SeekStage.__new__(SeekStage)
        stage.llm = None
        stage.config = None
        loop = stage._make_agent_loop()
        from haa.harness.agent_loop import AgentLoop as HarnessLoop
        assert isinstance(loop, HarnessLoop)
        assert "read_file" in loop.tools.names()
        # 空阶段上下文放行（旧语义）
        assert loop.tools.get_schemas("") != []
