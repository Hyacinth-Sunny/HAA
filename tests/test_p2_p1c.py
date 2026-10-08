"""批次8（P2-a/b 自跑链）+ 批次9（P1-c 分支树+分型对决）测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from haa.branch_duel import (
    BranchNode,
    BranchTree,
    DuelResult,
    classify_paper_type,
    compute_duel_rankings,
    parse_duel_verdict,
    swiss_pairings,
)
from haa.config import Config, HarnessConfig, StorageConfig
from haa.models import Brief, Campaign, Candidate
from haa.settlement import SettlementManager
from haa.harness.session_log import SessionEventLog, _RecordingSink


# ==================== 批次9：分支树 ====================

def _cand(slug, title, score=0.5):
    return Candidate(campaign_id="c", slug=slug, title=title,
                     significance=score, win_odds=score, difficulty=0.3,
                     queue_index=0)


def test_branch_tree_add_kill_next_alive():
    tree = BranchTree(max_branches=3)
    a = tree.add_root(_cand("alpha", "Alpha", 0.8))
    b = tree.add_root(_cand("beta", "Beta", 0.6))
    c = tree.add_root(_cand("gamma", "Gamma", 0.7))
    assert tree.alive_count() == 3
    tree.kill_branch("alpha", stage="VERIFY", reason="反例 x<0")
    assert tree.alive_count() == 2
    assert a.status == "dead" and a.kill_reason == "反例 x<0"
    nxt = tree.next_alive()
    assert nxt.slug == "gamma"  # 分数最高的 alive (0.7 > 0.6)
    tree.kill_branch("gamma", stage="PILOT", reason="not_supported")
    tree.kill_branch("beta", stage="NOVELTY", reason="SOLVED")
    assert tree.alive_count() == 0
    assert tree.next_alive() is None  # 全 dead → 队列耗尽


def test_branch_tree_budget_gate():
    tree = BranchTree(max_branches=2, budget_factor=0.6)
    tree.add_root(_cand("a", "A"))
    tree.add_root(_cand("b", "B"))
    assert not tree.can_spawn_branch(base_budget=1.0)  # 已到 max_branches=2
    tree2 = BranchTree(max_branches=5, budget_factor=0.6)
    tree2.add_root(_cand("a", "A"))
    assert tree2.can_spawn_branch(base_budget=1.0)  # 还有余量


def test_branch_tree_view_renders_dead_reason():
    tree = BranchTree()
    tree.add_root(_cand("alpha", "Alpha Design"))
    tree.kill_branch("alpha", stage="SCREEN", reason="counterexample: x=−1")
    view = tree.render_tree_view()
    assert "✗" in view and "Alpha" in view
    assert "SCREEN" in view and "counterexample" in view


def test_pipeline_records_branch_tree_on_kill(tmp_path):
    """_record_kill 同步标记分支树（feature branch_tree 开时）。"""
    from haa.pipeline import Pipeline, StageName
    from haa.stages.base import StageContext, StageResult, StageStatus
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "camps")),
                 harness=HarnessConfig(features=(("branch_tree", True),)))
    store = StateStore(tmp_path / "t.db")
    pipe = Pipeline(cfg, store, MagicMock(), llm=MagicMock(),
                    stages={s: MagicMock() for s in StageName})
    brief = Brief(title="B", problem_area="P")
    camp = store.create_campaign(brief)
    context = StageContext(brief=brief)
    # 初始化分支树
    from haa.branch_duel import BranchTree
    tree = BranchTree()
    c1 = _cand("alpha", "Alpha")
    tree.add_root(c1)
    context.extra["branch_tree"] = tree
    context.candidate = c1
    # 模拟 kill
    result = StageResult(status=StageStatus.ABORT_CANDIDATE,
                         data={"reason": "novelty: SOLVED"})
    pipe._record_kill("NOVELTY", context, result)
    assert tree.roots[0].status == "dead"
    assert "SOLVED" in tree.roots[0].kill_reason


from haa.state import StateStore


# ==================== 批次9：分型对决 ====================

def test_classify_paper_type():
    assert classify_paper_type("A Proof of Lower Bounds") == "theory"
    assert classify_paper_type("New Benchmark Dataset") == "benchmark"
    assert classify_paper_type("System Architecture for X") == "systems"
    assert classify_paper_type("Experimental Study of Y") == "experimental"


def test_swiss_pairings_same_type():
    cands = [
        {"slug": "a", "title": "Proof A", "rationale": "proof theory"},
        {"slug": "b", "title": "Proof B", "rationale": "theorem proof"},
        {"slug": "c", "title": "System C", "rationale": "system architecture"},
        {"slug": "d", "title": "System D", "rationale": "deployment system"},
    ]
    import random
    pairs = swiss_pairings(cands, rng=random.Random(42))
    assert len(pairs) == 2
    # 同型配对：a-b 或 b-a（theory），c-d 或 d-c（systems）
    slugs = {(p[0]["slug"], p[1]["slug"]) for p in pairs}
    assert ("a", "b") in slugs or ("b", "a") in slugs
    assert ("c", "d") in slugs or ("d", "c") in slugs


def test_parse_duel_verdict():
    r = parse_duel_verdict('{"winner": "甲", "reason": "stronger"}',
                           "slug_a", "slug_b", first_is_a=True)
    assert r is not None and r.winner == "slug_a"
    r2 = parse_duel_verdict('{"winner": "乙", "reason": "clear"}',
                            "slug_a", "slug_b", first_is_a=False)
    assert r2 is not None and r2.winner == "slug_a"  # 乙=first=slug_a
    assert parse_duel_verdict('{"winner": "相当"}', "a", "b", True) is None


def test_compute_duel_rankings():
    cands = [{"slug": "a", "title": "A"}, {"slug": "b", "title": "B"},
             {"slug": "c", "title": "C"}]
    duels = [DuelResult(winner="a", loser="b", reason=""),
             DuelResult(winner="a", loser="c", reason=""),
             DuelResult(winner="b", loser="c", reason="")]
    ranked = compute_duel_rankings(cands, duels)
    assert ranked[0]["slug"] == "a" and ranked[0]["wins"] == 2
    assert ranked[1]["slug"] == "b" and ranked[2]["slug"] == "c"


def test_grade_outputs_paper_type(tmp_path):
    from haa.stages import GradeStage
    from haa.stages.base import StageContext
    from haa.llm.agent_loop import AgentLoopResult
    cfg = Config(storage=StorageConfig(db_path=str(tmp_path / "t.db"),
                                       campaigns_dir=str(tmp_path / "camps")))
    stage = GradeStage(llm=None, config=cfg)
    content = json.dumps({"grade": "SOLID", "rationale": "ok"})
    stage._make_agent_loop = lambda: SimpleNamespace(
        run=lambda *a, **kw: AgentLoopResult(content=content))
    camp = Campaign(brief_hash="abc")
    cand = _cand("proof-x", "A Proof of Impossibility")
    ctx = StageContext(brief=Brief(title="B", problem_area="P"), candidate=cand)
    result = stage.run(camp, ctx)
    assert result.data["paper_type"] == "theory"


# ==================== 批次8：自跑链 ====================

def test_self_runner_generate_code(tmp_path):
    from haa.p2.self_runner import SelfRunningCoder
    coder = SelfRunningCoder(campaigns_dir=tmp_path)
    # Mock LLM: 返回让 agent 用 write_file 写 solve.sh 的响应
    from haa.harness.registry import ToolRegistry

    # 直接测 registry 的 write_file 能在 work_dir 下写文件
    work = tmp_path / "test_exp"
    work.mkdir()
    r = coder.registry.invoke("write_file", {
        "path": str(work / "solve.sh"),
        "content": "#!/bin/bash\necho hello\n",
    })
    assert (work / "solve.sh").exists()


def test_fallback_acp_still_gated():
    """ACP 默认不可达（D2），自跑链为默认路径。"""
    from haa.config import default_config
    cfg = default_config()
    assert cfg.acp.enabled is False
    assert cfg.harness.feature("fallback_acp") is False


# ==================== 实验设计冻结注入（第四章 §3.3） ====================

def test_analyze_prompt_includes_frozen_design():
    """ANALYZE 上下文必须含冻结的实验设计+假设（p2_analyze prompt 验证）。"""
    from haa.prompts import render_prompt, has_prompt
    assert has_prompt("p2_analyze")
    text = render_prompt("p2_analyze",
                         exp_spec={"hypothesis": "test"},
                         metrics={"acc": 0.9},
                         debug_log="tail")
    # prompt 应引用实验设计/假设（冻结注入—— Finch 落点）
    assert "exp_spec" in text.lower() or "hypothesis" in text.lower() \
        or "实验设计" in text or "设计" in text
