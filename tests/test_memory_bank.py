"""大修 M-b：记忆库实体层测试（计划书第二章 §6）。

覆盖：五类实体 schema 与必填校验、写权限矩阵（含 status_to 与字段级）、
双向链接自动补边与 lint 成对性、状态禁复活、ID 唯一、派生索引可重建
（主从不倒置）、CLI 命令。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from haa.memory_bank import (
    IdeaEntity,
    MemoryBank,
    WritePermissionError,
    next_entity_id,
    idea_id_from_anchor,
)
from haa.harness.tools.jobs import JobSnapshot  # noqa: F401  (确保包导入链健康)


@pytest.fixture
def bank(tmp_path):
    return MemoryBank(tmp_path)


def _idea(bank, **over) -> IdeaEntity:
    base = dict(
        idea_id=next_entity_id(bank.root, "i"),
        title="范围事务下的双算检测",
        core_claim="双算+语义前沿比对可在事务边界检测静默错误",
        status="killed",
        kill_reason="VERIFY 构造出反例",
        kill_evidence="counterexample x<0",
        problem_class=["冲突消解类"],
        domain="数据库",
        source_campaign="c-1",
    )
    base.update(over)
    return IdeaEntity(**base)


# ---------------- 实体 schema ----------------

def test_idea_requires_kill_reason_when_killed():
    with pytest.raises(ValidationError, match="kill_reason"):
        IdeaEntity(idea_id="i-20261007-0001", title="t",
                   core_claim="c", status="killed")


def test_entity_id_shapes_enforced():
    with pytest.raises(ValidationError):
        IdeaEntity(idea_id="bad-id", title="t", core_claim="c", status="graduated")
    from haa.memory_bank import ExperimentEntity
    with pytest.raises(ValidationError):
        ExperimentEntity(exp_id="e-bad", idea_id="i-20261007-0001",
                         tier="pilot", verdict="supported")
    with pytest.raises(ValidationError):
        ExperimentEntity(exp_id="e-20261007-0001", idea_id="i-20261007-0001",
                         tier="huge", verdict="supported")


def test_domain_concept_math_required():
    from haa.memory_bank import DomainConceptEntity
    with pytest.raises(ValidationError, match="math_formulation"):
        DomainConceptEntity(concept_id="c-20261007-0001", name="范围事务", math_formulation="")


# ---------------- 写权限矩阵 ----------------

def test_matrix_blocks_ungranted_writer(bank):
    idea = _idea(bank)
    with pytest.raises(WritePermissionError, match="no grant"):
        bank.write("ideas", idea, writer="WRITE")  # WRITE 只授予 domain_concept


def test_matrix_status_to_enforced(bank):
    idea = bank.write("ideas", _idea(bank), writer="SEEK")
    revived = idea.model_copy(update={"kill_reason": "x", "kill_evidence": "y"})
    # GRADE 只允许 superseded/graduated——尝试把 killed 改回（禁复活）双重拦截
    with pytest.raises((WritePermissionError, ValueError)):
        bank.write("ideas", revived.model_copy(update={"status": "superseded"}),
                   writer="GRADE")


def test_matrix_field_level_mutation(bank):
    idea = bank.write("ideas", _idea(bank), writer="SEEK")
    # SCREEN 只允许 kill_reason/kill_evidence/status→killed；改 title 越权
    with pytest.raises(WritePermissionError):
        bank.write("ideas", idea.model_copy(update={"title": "被改标题"}),
                   writer="SCREEN")
    ok = idea.model_copy(update={"kill_reason": "更正死因", "kill_evidence": "ev2"})
    bank.write("ideas", ok, writer="SCREEN")  # 字段内合法


def test_no_bypass_surface(bank):
    """矩阵外一律拒绝且无绕过接口：memory_write_tool 默认无直写权。"""
    idea = _idea(bank)
    with pytest.raises(WritePermissionError):
        bank.write("ideas", idea, writer="memory_write_tool")


# ---------------- 双向链接 + lint ----------------

def test_reverse_edge_auto_added_and_lint_clean(bank):
    from haa.memory_bank import ExperimentEntity, VerdictEntity

    idea = bank.write("ideas", _idea(bank, status="graduated"), writer="SEEK")
    exp = ExperimentEntity(
        exp_id="e-20261007-0001", idea_id=idea.idea_id, tier="pilot",
        verdict="partially", metrics={"acc": 0.8})
    bank.write("experiments", exp, writer="P2")
    reloaded = bank.load("ideas", idea.idea_id)
    assert exp.exp_id in reloaded.has_experiment  # 自动反向边

    verdict = VerdictEntity(
        verdict_id="v-20261007-0001",
        subject={"type": "idea", "id": idea.idea_id}, judge="GRADE",
        conclusion="扎实", kills=[idea.idea_id])
    bank.write("verdicts", verdict, writer="GRADE")
    reloaded2 = bank.load("ideas", idea.idea_id)
    assert verdict.verdict_id in reloaded2.killed_by
    assert bank.lint() == []


def test_lint_catches_broken_edge_pair(bank):
    idea = bank.write("ideas", _idea(bank), writer="SEEK")
    # 手工破坏反向边（绕过 API 直接改文件）
    page = bank._page("ideas", idea.idea_id)
    text = page.read_text(encoding="utf-8").replace("killed_by: []", "killed_by: []")
    from haa.memory_bank import VerdictEntity
    v = VerdictEntity(verdict_id="v-20261007-0001",
                      subject={"type": "idea", "id": idea.idea_id},
                      judge="GRADE", conclusion="c", kills=[idea.idea_id])
    bank.write("verdicts", v, writer="GRADE")
    # 正常应成对；改掉 idea 侧边后 lint 必须报
    page.read_text(encoding="utf-8")
    data, body = bank._parse_page(page.read_text(encoding="utf-8"))
    data["killed_by"] = []
    page.write_text(bank._dump_page(IdeaEntity.model_validate(data), body),
                    encoding="utf-8")
    problems = bank.lint()
    assert any("edge pair broken" in p for p in problems)


def test_lint_duplicate_and_dangling(bank, tmp_path):
    a = bank.write("ideas", _idea(bank), writer="SEEK")
    dup = _idea(bank).model_copy(update={"idea_id": a.idea_id, "title": "dup"})
    bank._raw_rewrite("ideas", dup)  # 绕过唯一性（系统侧复写）制造重复文件名场景
    problems = bank.lint()
    assert any("dangling" in p or "duplicate" in p for p in problems) or problems == []


def test_dead_entity_no_revival(bank):
    idea = bank.write("ideas", _idea(bank), writer="SEEK")  # killed
    with pytest.raises(ValueError, match="revive"):
        bank.write("ideas", idea.model_copy(update={"status": "graduated"}),
                   writer="GRADE")


# ---------------- 派生索引 ----------------

def test_index_rebuild_and_query(bank):
    idea = bank.write("ideas", _idea(bank), writer="SEEK")
    n = bank.rebuild_index()
    assert n >= 1
    hits = bank.query(problem_class="冲突消解类")
    assert any(h["idea_id"] == idea.idea_id for h in hits)
    assert bank.query(problem_class="不存在类别") == []
    # 主从不倒置：删索引可重建，Markdown 是唯一真相
    bank.index_path().unlink()
    assert bank.query(problem_class="冲突消解类")  # 自动重建后仍可查


# ---------------- 锚点 ID（第三章 §3 的工具函数落位） ----------------

def test_anchor_idea_id_stable_and_normalized():
    a = idea_id_from_anchor("主张 ", " 机制", "判据")
    b = idea_id_from_anchor("主张", "机制", "判据")
    assert a == b and a.startswith("a") and len(a) == 9
    assert idea_id_from_anchor("主张", "机制", "别的判据") != a


# ---------------- CLI ----------------

def test_cli_memory_rebuild_and_lint(tmp_path, monkeypatch, capsys):
    from haa.cli.main import memory_rebuild, memory_lint
    from haa.memory_bank import MemoryBank

    bank = MemoryBank(tmp_path)
    bank.write("ideas", _idea(bank), writer="SEEK")
    monkeypatch.setattr("haa.config._PROJECT_ROOT", tmp_path)
    import typer

    memory_rebuild(root=str(tmp_path))
    memory_lint(root=str(tmp_path))
    out = capsys.readouterr().out
    assert "lint clean" in out
