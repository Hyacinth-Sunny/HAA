"""Tests for the dead-format brief parser（v1.0.4：固定结构 md → Brief，零 LLM）."""

from __future__ import annotations

import pytest

from haa.brief_schema import DeadFormatError, parse_dead_format, try_parse_dead_format


_MINI = """# 研究简报：测试题

**报告日期：** 2026-08-25
**赛道：** systems

#### 一、问题阐述

要解决 X 问题，材料见 `/notes/a.md`。

#### 二、重难点分析

难在 Y。

#### 四、 产出要求（委托的交付物）

交付物是整合设计。

**硬约束**

- ML 只进优化层
- RTO 分桶报告

**明确不做**

- 不做日志回放加速

#### 五、思路拟定

先 A 后 B。
"""


def test_parse_happy_path():
    b = parse_dead_format(_MINI)
    assert b.title == "研究简报：测试题"
    assert b.track.value == "systems"
    assert b.constraints == ["ML 只进优化层", "RTO 分桶报告"]
    assert b.exclusions == ["不做日志回放加速"]
    assert b.knowledge_files == ["/notes/a.md"]
    # problem_area 原文拼接：五节中四节有正文（三缺省），硬约束 bullet 不重复计入
    for kw in ("问题阐述", "重难点分析", "产出要求", "思路拟定", "要解决 X 问题"):
        assert kw in b.problem_area
    assert "ML 只进优化层" not in b.problem_area  # 结构化字段不进正文


def test_parse_defaults_theory_without_meta():
    doc = _MINI.replace("**赛道：** systems", "")
    assert parse_dead_format(doc).track.value == "theory"


def test_heading_subsection_variant_also_recognized():
    """`### 硬约束` 标题式与 `**硬约束**` 加粗式都识别。"""
    doc = _MINI.replace("**硬约束**", "### 硬约束").replace("**明确不做**", "#### 排除方向")
    b = parse_dead_format(doc)
    assert b.constraints == ["ML 只进优化层", "RTO 分桶报告"]
    assert b.exclusions == ["不做日志回放加速"]


def test_missing_constraints_bullet_raises():
    doc = _MINI.replace("**硬约束**\n\n- ML 只进优化层\n- RTO 分桶报告\n", "")
    with pytest.raises(DeadFormatError):
        parse_dead_format(doc)


def test_missing_required_section_raises():
    with pytest.raises(DeadFormatError):
        parse_dead_format(_MINI.replace("#### 四、 产出要求（委托的交付物）", "#### 四、 产出"))
    # ↑ 节名不带"产出要求"前缀 → 必有节缺失
    with pytest.raises(DeadFormatError):
        parse_dead_format(_MINI.replace("#### 一、问题阐述", "#### 〇、背景"))


def test_freeform_returns_none_for_fallback():
    """自由格式（无节结构）→ None → load_brief 走编译器兜底。"""
    assert try_parse_dead_format("随便写的一篇散文，没有任何固定结构。") is None


def test_load_brief_md_prefers_dead_format(tmp_path, monkeypatch):
    """统一入口：.md 先走确定性解析（零 LLM）。"""
    import haa.brief_compiler as bc
    import haa.brief_io as bio

    def _no_llm(text, client):
        raise AssertionError("dead-format 命中时不应调用编译器")

    monkeypatch.setattr(bc, "compile_brief", _no_llm)
    p = tmp_path / "brief.md"
    p.write_text(_MINI, encoding="utf-8")
    b = bio.load_brief(p)
    assert b.constraints == ["ML 只进优化层", "RTO 分桶报告"]


def test_numbered_bullets_accepted():
    doc = _MINI.replace(
        "- ML 只进优化层\n- RTO 分桶报告",
        "1. ML 只进优化层\n2. RTO 分桶报告",
    )
    assert parse_dead_format(doc).constraints == ["ML 只进优化层", "RTO 分桶报告"]
