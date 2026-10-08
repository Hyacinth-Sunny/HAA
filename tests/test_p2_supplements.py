"""增补三件测试：机型匹配、概念回填、冻结注入纪律。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from haa.p2.self_runner import (
    SelfRunningCoder,
    backfill_concepts,
    build_concept_backfill_block,
    check_machine_match,
    parse_code_refs_backfill,
)


# ==================== ① 机型匹配检查 ====================

def test_no_requirements_passes():
    assert check_machine_match({}) == []
    assert check_machine_match({"resource_estimate": {}}) == []


def test_gpu_mismatch_warned():
    spec = {"resource_estimate": {"gpu_memory_gb": 24, "ram_gb": 64}}
    warnings = check_machine_match(spec, local_mode=True)
    assert any("gpu_memory_gb" in w for w in warnings)
    assert any("ram_gb" in w for w in warnings)


def test_within_limits_no_warning():
    spec = {"resource_estimate": {"cpu_cores": 4, "ram_gb": 8}}
    assert check_machine_match(spec, local_mode=True) == []


def test_server_profile_match():
    profile = SimpleNamespace(gpu_memory_gb=48, cpu_cores=32, ram_gb=256,
                               cuda_version="12.4", disk_gb=1000)
    spec = {"resource_estimate": {"gpu_memory_gb": 24, "cuda_version": "12.0"}}
    assert check_machine_match(spec, profile, local_mode=False) == []


def test_server_profile_cuda_mismatch():
    profile = SimpleNamespace(gpu_memory_gb=16, cpu_cores=8, ram_gb=32,
                               cuda_version="11.8", disk_gb=100)
    spec = {"resource_estimate": {"cuda_version": "12.4"}}
    warnings = check_machine_match(spec, profile, local_mode=False)
    assert any("cuda" in w.lower() for w in warnings)


# ==================== ② 概念档案回填 ====================

CONCEPTS = [
    {"concept_id": "C1", "name": "范围事务", "math_formulation": "$Txn(r,w)$",
     "code_refs": [], "status": "defined"},
    {"concept_id": "C2", "name": "已回填概念", "math_formulation": "$\\phi$",
     "code_refs": [{"repo": "x", "path": "y", "symbol": "z"}],
     "status": "imported"},
]


def test_build_backfill_block_only_empty():
    block = build_concept_backfill_block(CONCEPTS)
    assert "范围事务" in block and "C1" in block
    assert "已回填概念" not in block  # 已有 code_refs 的不进
    assert "⛓⛓⛓" in block
    assert build_concept_backfill_block([]) == ""


def test_parse_backfill_json():
    text = 'Some output text... {"code_refs_backfill": {"C1": [{"repo": "exp", "path": "code/rt.py", "symbol": "RangeTxn"}]}}'
    refs = parse_code_refs_backfill(text)
    assert "C1" in refs and refs["C1"][0]["symbol"] == "RangeTxn"
    assert parse_code_refs_backfill("no json here") == {}


def test_backfill_concepts_writes_refs():
    backfill = {"C1": [{"repo": "exp", "path": "code/rt.py", "symbol": "RT"}]}
    updated = backfill_concepts(CONCEPTS, backfill)
    assert updated[0]["code_refs"][0]["symbol"] == "RT"
    assert updated[1]["code_refs"][0]["repo"] == "x"  # 已有的不动


# ==================== ③ 冻结注入纪律 ====================

def test_analyze_prompt_has_frozen_discipline():
    from haa.prompts import render_prompt
    text = render_prompt(
        "p2_analyze",
        precursor=SimpleNamespace(
            candidate_title="Test",
            paper={"abstract": "abs"},
            candidate=SimpleNamespace(
                positive_claim="双算检测静默错误",
                negative_claim="误报为零"),
        ),
        exp_spec={"hypothesis": "test"},
        metrics={"acc": 0.9},
        log_tail="tail",
    )
    assert "⛓⛓⛓ 冻结的实验设计与假设" in text
    assert "禁止偏离冻结设计分析" in text
    assert "双算检测静默错误" in text
    assert "误报为零" in text
    assert "缺失的实验=偏离" in text


def test_analyze_prompt_claims_mapping_required():
    from haa.prompts import render_prompt
    text = render_prompt("p2_analyze",
                         precursor=None, exp_spec={},
                         metrics={}, log_tail="")
    assert "逐条映射" in text
    assert "冻结" in text
