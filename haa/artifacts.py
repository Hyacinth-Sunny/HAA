"""Artifact store —— 中间产出落盘（v0.9.1）。

设计原则（见 PlanMode 设计讨论）：
- **上下文仍是状态机的传输层**（隔离保住：换候选时 ``_reset_candidate_context``
  确定性清空，文件系统不会有的"后推前"泄漏继续被避免）。
- 每次 ``Pipeline._persist`` 同步把结构化产出**幂等落盘**为工件 + MANIFEST，
  供 (a) 用户/GUI 直接查看 (b) read_file 报错时列实际文件治幻觉
  (c) 外置模块（AutoSci 记忆、OpenReview 评分、知识注入）经这一个接缝集成。

目录布局::

    campaigns/<cid>/artifacts/
      ├── MANIFEST.json                  # [{path, stage, candidate_slug, updated_at}]
      ├── candidates.json                 # SEEK:done → 完整候选列表（含 filtered/dead）
      ├── kills.json                      # extra.kills → 击杀档案
      └── <candidate_slug>/<stage>.json   # novelty/screen/grade/review/paper/
                                          #   exp_spec/verify_findings
    campaigns/<cid>/artifacts/_rejected/<slug>/…   # 被杀候选的工件归档（ARV 可复核）

写失败永不抛出（工件是可观测性附加层，不能影响主管线）。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("haa.artifacts")

# 各 stage 落盘的 context 键 → 工件文件名
_STAGE_FILES: dict[str, str] = {
    "NOVELTY": "novelty",
    "SCREEN": "screen",
    "GRADE": "grade",
    "REVIEW": "review",
    "WRITE": "paper",
    "REFINE": "paper",          # 精修后的最新 paper 覆盖同名工件
    "EXP_SPEC": "exp_spec",     # 存 extra.exp_spec
    "VERIFY": "verify",         # verify_findings + verify_passed
    "EXP_FEASIBILITY": "exp_feasibility",
    "PILOT": "pilot",
    "ANALYZE": "analysis",  # 实验分析+观点终审（§3.1）  # 先导实验判决（M2）  # extra.exp_findings（blockers）
}


def _artifacts_dir(campaigns_dir: str | Path, campaign_id: str) -> Path:
    return Path(campaigns_dir) / campaign_id / "artifacts"


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_artifacts(
    campaigns_dir: str | Path,
    campaign_id: str,
    context: Any,
    label: str,
) -> None:
    """幂等落盘：把 StageContext 的结构化产出写成工件 + 更新 MANIFEST。

    只在 ``:done`` / 终态 label 时调用（``:start`` 时上下文未变，跳过）。
    任何异常吞掉并 log——工件层绝不影响主管线。
    """
    if not label.endswith(":done") and ":" not in label:
        # 终态（PUBLISHED / RETIRED:xxx）无冒号或以其它形式出现——也写。
        pass
    if label.endswith(":start"):
        return
    try:
        base = _artifacts_dir(campaigns_dir, campaign_id)
        stage = label.split(":")[0]
        written: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc).isoformat()

        def _record(rel: str) -> None:
            written.append({
                "path": rel, "stage": stage,
                "candidate_slug": getattr(context.candidate, "slug", None),
                "updated_at": now,
            })

        # --- SEEK：完整候选清单（含 filtered/dead——用户要看被否决的 idea） ---
        if stage == "SEEK" and context.candidates:
            _dump(base / "candidates.json", [
                c.model_dump(mode="json") if hasattr(c, "model_dump") else c
                for c in context.candidates
            ])
            _record("candidates.json")

        # --- 逐候选工件 ---
        slug = getattr(context.candidate, "slug", None)
        if slug and stage in _STAGE_FILES:
            fname = _STAGE_FILES[stage]
            payload = _stage_payload(stage, context)
            if payload is not None:
                _dump(base / slug / f"{fname}.json", payload)
                _record(f"{slug}/{fname}.json")

        # --- 击杀档案（累积，任何阶段都可能追加） ---
        kills = (getattr(context, "extra", None) or {}).get("kills") or []
        if kills:
            _dump(base / "kills.json", kills)
            _record("kills.json")

        # --- MANIFEST：合并既有条目（不同阶段/候选各自追加） ---
        _update_manifest(base, written)
    except Exception as exc:  # noqa: BLE001 — 可观测性附加层不得影响主管线
        logger.warning("write_artifacts failed (%s): %s", label, exc)


def _stage_payload(stage: str, context: Any) -> Any:
    """提取该 stage 在 context 上的结构化产出；无内容返回 None。"""
    # WRITE/REFINE 的产出都落在 context.paper（smoke4 复盘：旧实现取
    # context.write/context.refine 恒为 None → paper.json 从未落盘过）。
    if stage in ("WRITE", "REFINE"):
        return getattr(context, "paper", None) or None
    if stage in ("NOVELTY", "SCREEN", "GRADE", "REVIEW"):
        return getattr(context, stage.lower(), None) or None
    if stage == "EXP_SPEC":
        return (getattr(context, "extra", None) or {}).get("exp_spec") or None
    if stage == "VERIFY":
        findings = getattr(context, "verify_findings", None) or []
        if not findings and not getattr(context, "verify_passed", False):
            return None
        return {
            "verify_passed": getattr(context, "verify_passed", False),
            "findings": findings,
        }
    if stage == "EXP_FEASIBILITY":
        return (getattr(context, "extra", None) or {}).get("exp_findings") or None
    if stage == "ANALYZE":
        # A3 批次18-2：ANALYZE 产物落盘（此前 _STAGE_FILES 有映射但 payload
        # 恒 None → analysis.json 从未写盘）。无 analysis 时跳过。
        return (getattr(context, "extra", None) or {}).get("analysis") or None
    return None


def _update_manifest(base: Path, new_entries: list[dict[str, Any]]) -> None:
    """合并写入：同 path 覆盖，新 path 追加。"""
    manifest_path = base / "MANIFEST.json"
    existing: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    by_path = {e.get("path"): e for e in existing}
    for entry in new_entries:
        by_path[entry["path"]] = entry
    ordered = sorted(by_path.values(), key=lambda e: (e.get("candidate_slug") or "", e.get("path")))
    _dump(manifest_path, ordered)


def archive_rejected_candidate(
    campaigns_dir: str | Path, campaign_id: str, slug: str
) -> None:
    """换候选时归档被杀候选的工件到 ``_rejected/<slug>/``（供 ARV 复核）。

    移动而非删除——镜像 `_reset_candidate_context` 的隔离语义（新候选
    的工作目录干净），但历史可查。
    """
    try:
        base = _artifacts_dir(campaigns_dir, campaign_id)
        src = base / slug
        if not src.exists():
            return
        dest = base / "_rejected" / slug
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
    except Exception as exc:  # noqa: BLE001
        logger.warning("archive_rejected_candidate(%s) failed: %s", slug, exc)
