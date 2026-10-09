"""P2/P3 修订（批次15/16/17）测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from haa.p2revise import (
    TriageVerdict,
    build_preflight_checklist,
    check_hold_flag,
    clear_hold_flag,
    probe_environment,
    render_progress,
    set_hold_flag,
    triage_failure,
    validate_claim_metric_mapping,
    validate_kill_reason,
    build_assist_request,
)
from haa.p3revise import (
    NO_EXPERIMENT_PLACEHOLDER,
    lint_readability,
    normalize_paper_v2,
    render_lint_report,
    validate_paper_sections,
)


# ==================== §2.1.1 环境探测 ====================

def test_probe_environment_returns_fields():
    probe = probe_environment()
    assert "cpu" in probe and "gpu" in probe and "mem" in probe
    assert "disk_mb" in probe and "sudo_noninteractive" in probe
    assert "network" in probe
    assert int(probe["cpu"]["logical"]) > 0  # 至少 1 核


# ==================== §2.1.2 预审清单 ====================

def test_preflight_gpu_blocker():
    probe = {"gpu": {"count": 0, "vendor": "", "vram_mb": 0},
             "mem": {"total_mb": 32000}, "disk_mb": 100000,
             "network": True, "sudo_noninteractive": True}
    items = build_preflight_checklist(
        probe, {"gpu_memory_gb": 24}, ["CUDA training"])
    assert any(i["severity"] == "blocker" and "GPU" in i["item"] for i in items)


def test_preflight_ram_blocker():
    probe = {"gpu": {"count": 1, "vendor": "nvidia", "vram_mb": 32000},
             "mem": {"total_mb": 16000}, "disk_mb": 100000,
             "network": True, "sudo_noninteractive": True}
    items = build_preflight_checklist(probe, {"ram_gb": 64}, [])
    assert any("RAM" in i["item"] for i in items)


def test_preflight_no_issues():
    probe = {"gpu": {"count": 1, "vendor": "nvidia", "vram_mb": 32000},
             "mem": {"total_mb": 128000}, "disk_mb": 500000,
             "network": True, "sudo_noninteractive": True}
    items = build_preflight_checklist(probe, {"gpu_memory_gb": 16}, [])
    assert len(items) == 0


# ==================== §2.1.3 切题性 ====================

def test_claim_metric_missing():
    errors = validate_claim_metric_mapping({})
    assert any("claims" in e for e in errors)


def test_claim_metric_unmapped():
    spec = {"claims": ["主张A", "主张B"],
            "claim_metric_map": {"主张A": ["acc"]}}
    errors = validate_claim_metric_mapping(spec)
    assert any("主张B" in e for e in errors)


def test_claim_metric_valid():
    spec = {"claims": ["主张A"],
            "claim_metric_map": {"主张A": ["acc", "latency"]}}
    assert validate_claim_metric_mapping(spec) == []


# ==================== §2.2 分诊器 ====================

def test_triage_assist():
    log = "sudo apt install\nsudo: a password is required\nerror"
    v = triage_failure(log)
    assert v["category"] == "assist"
    assert any("sudo" in e.lower() for e in v["evidence_lines"])


def test_triage_environment():
    log = "Training epoch 3\nCUDA out of memory\nTried to allocate 2.00 GiB"
    v = triage_failure(log)
    assert v["category"] == "environment"


def test_triage_viewpoint():
    metrics = {"speedup": 0.7}  # < 1 = 反主张
    log = "Training complete\nspeedup ratio < 1"
    v = triage_failure(log, metrics=metrics)
    assert v["category"] in ("viewpoint_pending", "code")  # NaN 或 speedup<1


def test_triage_nan():
    metrics = {"loss": float("nan")}
    v = triage_failure("done", metrics=metrics)
    assert v["category"] == "viewpoint_pending"


def test_triage_default_code():
    v = triage_failure("Traceback (most recent call last):\n  File ...\nError")
    assert v["category"] == "code"


# ==================== §2.2.3 kill_reason 枚举 ====================

def test_kill_reason_valid():
    assert validate_kill_reason("assist")
    assert validate_kill_reason("environment")
    assert validate_kill_reason("budget")
    assert validate_kill_reason("legacy:old_reason")


def test_kill_reason_invalid():
    assert not validate_kill_reason("random_text")
    assert not validate_kill_reason("")


# ==================== §2.3 HOLD ====================

def test_hold_flag_set_clear(tmp_path):
    cid = "test_campaign"
    # 清理
    clear_hold_flag(cid, campaigns_dir=tmp_path)
    assert not check_hold_flag(cid, campaigns_dir=tmp_path)
    # 设标志
    set_hold_flag(cid, note="用户注记：索引建反了", campaigns_dir=tmp_path)
    assert check_hold_flag(cid, campaigns_dir=tmp_path)
    # 清除并取回注记
    note = clear_hold_flag(cid, campaigns_dir=tmp_path)
    assert note == "用户注记：索引建反了"
    assert not check_hold_flag(cid, campaigns_dir=tmp_path)


def test_assist_request_three_elements():
    v = TriageVerdict("assist", ["L3: sudo password required"],
                      "需要用户配置权限", "安装 CUDA 驱动")
    req = build_assist_request(v)
    assert "卡在哪" in req and "需要什么" in req and "证据" in req
    assert "恢复" in req


# ==================== §2.4 进度视图 ====================

def test_progress_view_empty():
    store = SimpleNamespace(list_campaigns=lambda: [])
    assert render_progress(store) == "(no campaigns)"


def test_progress_view_with_data(tmp_path):
    from haa.state import StateStore
    from haa.models import Brief
    store = StateStore(tmp_path / "t.db")
    camp = store.create_campaign(Brief(title="T", problem_area="P"))
    store.save_event(event_type="llm_call", campaign_id=camp.id,
                     stage="SEEK", cost_usd=0.01, tokens=100, payload={})
    store.save_event(event_type="tool_call", campaign_id=camp.id,
                     stage="SEEK", payload={"tool": "grep"})
    text = render_progress(store, campaign_id=camp.id)
    assert "SEEK" in text and "grep" in text


# ==================== §3.2 六节 schema ====================

def test_six_section_validation():
    paper = {"abstract": "a", "intro": "i", "background": "b", "method": "m"}
    errors = validate_paper_sections(paper)
    assert any("results" in e for e in errors)
    assert any("conclusion" in e for e in errors)


def test_no_experiment_placeholder():
    paper = {"abstract": "a", "intro": "i", "background": "b", "method": "m"}
    normalized = normalize_paper_v2(paper)
    assert NO_EXPERIMENT_PLACEHOLDER in normalized["results"]
    assert NO_EXPERIMENT_PLACEHOLDER in normalized["conclusion"]


# ==================== §3.4 可读性 lint ====================

def test_long_chinese_sentence():
    long_sent = "这是一段" * 15 + "超长句子"  # >60 中文字
    issues = lint_readability(long_sent)
    assert any(i["type"] == "long_sentence" for i in issues)


def test_normal_text_passes():
    text = "This is a normal sentence. It has clear meaning.\n这是正常句子。"
    issues = lint_readability(text)
    assert not any(i["type"] == "long_sentence" for i in issues)


def test_undefined_abbreviation():
    text = "We use HDBSMGT for storage management in this paper."
    issues = lint_readability(text)
    assert any(i["type"] == "undefined_abbrev" for i in issues)


def test_defined_abbreviation_passes():
    text = "We use API (Application Programming Interface) for this."
    issues = lint_readability(text)
    assert not any(i["type"] == "undefined_abbrev" and "API" in i["detail"]
                   for i in issues)


def test_lint_report_renders():
    issues = lint_readability("这是一段" * 20)
    report = render_lint_report(issues)
    if issues:
        assert "问题" in report
    else:
        assert "通过" in report
