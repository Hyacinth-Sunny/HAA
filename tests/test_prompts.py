"""Tests for the prompt-template layer (haa/prompts.py + prompts/*.md).

Covers: all expected templates exist, each renders with a full context and with
an all-None context (the latter is the early-pipeline case where most context
objects are legitimately None).
"""

from __future__ import annotations

import pytest

from haa.models import Brief, Candidate
from haa.prompts import has_prompt, prompt_names, render_prompt

EXPECTED = {
    "seek", "novelty", "screen", "design", "verify", "grade",
    "write", "refine",
    "review/correctness", "review/quality", "review/industry",
}


def test_all_expected_prompts_present():
    found = set(prompt_names())
    missing = EXPECTED - found
    assert not missing, f"missing prompt templates: {missing}"
    for name in EXPECTED:
        assert has_prompt(name)


def _full_context():
    brief = Brief(
        title="Fast convolution upper bound",
        problem_area="convolution complexity",
        constraints=["must be closed-form"],
        exclusions=["no incremental future-work variants"],
    )
    cand = Candidate(
        campaign_id="c", slug="fast-conv", title="Fast conv",
        significance=0.8, win_odds=0.6, difficulty=0.4, queue_index=0,
        positive_claim="we achieve O(n log n)", negative_claim="Omega(n) lower bound",
        attack_plan="divide and conquer",
    )
    return dict(
        brief=brief,
        candidate=cand,
        design={
            "plan": "divide and conquer", "positive_claim": "pc", "negative_claim": "nc",
            "obligations": ["ob1", "ob2"], "key_lemmas": ["lemma A"],
        },
        verify_findings=[{"kind": "counterexample", "detail": "fails for n=2"}],
        design_round=2,
        verify_passed=True,
        review={"reports": {
            "correctness": {"score": 0.5, "verdict": "sketchy",
                            "major_issues": ["proof is a sketch"]},
        }},
        paper={
            "title": "Fast Conv", "abstract": "abs", "intro": "i", "method": "m",
            "eval": "e", "related": "r", "conclusion": "c",
        },
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_prompt_renders_with_full_context(name):
    out = render_prompt(name, **_full_context())
    assert isinstance(out, str)
    assert len(out) > 50, f"{name} rendered too short"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_prompt_renders_with_none_context(name):
    # Early-pipeline case: nothing populated yet.
    out = render_prompt(
        name,
        brief=None, candidate=None, design=None, review=None, paper=None,
        verify_findings=[], design_round=0, verify_passed=False,
    )
    assert isinstance(out, str)


def test_refine_injects_paper_sections():
    out = render_prompt("refine", paper={"title": "T", "method": "THE METHOD BODY"}, review=None)
    assert "THE METHOD BODY" in out  # section content actually injected


def test_seek_injects_brief():
    out = render_prompt("seek", brief=Brief(title="UNIQUE_TITLE_X", problem_area="pa"))
    assert "UNIQUE_TITLE_X" in out


def test_prompt_cache_returns_same_template():
    # Rendering twice should reuse the cached template (no reload), same output.
    a = render_prompt("grade", candidate=None, design=None, verify_findings=[], verify_passed=True)
    b = render_prompt("grade", candidate=None, design=None, verify_findings=[], verify_passed=True)
    assert a == b


# --- v1.0.6-rev4: 赛道分叉与措辞-配置对齐（smoke6/8 教训锚定） -------------------


def test_seek_candidate_count_rendered():
    # A5: prompt 的候选数量必须与 pipeline.seek_candidate_count 一致，
    # 不再是硬编码"4–6 个"（多出的候选被静默 FILTERED）。
    out = render_prompt(
        "seek", brief=Brief(title="T", problem_area="pa"), candidate_count=3
    )
    assert "3 个" in out and "4–6" not in out


def test_seek_candidate_unit_clause_present():
    # A3: "一个候选=简报委托的完整交付"条款（smoke6 三问切片教训）。
    out = render_prompt(
        "seek", brief=Brief(title="T", problem_area="pa"), candidate_count=3
    )
    assert "完整交付" in out and "同时覆盖全部问题" in out


def test_seek_systems_track_branch():
    # A1: systems 赛道渲染负面主张翻译（红线+证伪条件）与定理禁令；
    # theory 赛道不出现该分支。
    sys_brief = Brief(title="T", problem_area="pa", track="systems")
    out_sys = render_prompt("seek", brief=sys_brief, candidate_count=3)
    assert "systems 赛道适配" in out_sys
    assert "证伪条件" in out_sys and "禁止" in out_sys
    out_theory = render_prompt(
        "seek", brief=Brief(title="T", problem_area="pa"), candidate_count=3
    )
    assert "systems 赛道适配" not in out_theory


def test_write_systems_track_branch():
    # A2: WRITE 的 systems 分叉（系统设计文档骨架/定理仅安全论证/实现要点）。
    from haa.models import Candidate

    cand = Candidate(
        campaign_id="c", slug="s", title="T", queue_index=0,
        significance=0.7, win_odds=0.6, difficulty=0.4,
    )
    sys_brief = Brief(title="T", problem_area="pa", track="systems")
    out_sys = render_prompt(
        "write", brief=sys_brief, candidate=cand, design=None, verify_passed=True
    )
    assert "系统设计文档" in out_sys and "实现要点" in out_sys
    assert "禁止为体裁自造新定理" in out_sys
    out_theory = render_prompt(
        "write", brief=Brief(title="T", problem_area="pa"), candidate=cand,
        design=None, verify_passed=True,
    )
    assert "系统设计文档" not in out_theory


def test_grade_repairable_counterexample_clause():
    # A4: VERIFY 遗留反例可修复 → thin 而非 loophole（防"可修被杀"）。
    out = render_prompt(
        "grade", candidate=None, design=None,
        verify_findings=[{"kind": "counterexample", "detail": "fails when n<2"}],
        verify_passed=False,
    )
    assert "可修复" in out and "thin" in out and "loophole" in out
