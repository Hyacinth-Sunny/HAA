"""B4: 事件日志轮转——campaign 终态后导出到 JSON 归档文件。

使用 StateStore.list_events / delete_campaign_events（SQL 在彼处）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("haa.event_rotation")

TERMINAL_STATUSES = frozenset({"published", "retired", "moribund"})


def rotate_events(store, campaign_id: str, *, campaigns_dir=None) -> int:
    """campaign 终态后归档其事件到 JSON 文件并从主表删除。"""
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        return 0
    status = getattr(campaign, "status", None)
    if status is None or str(status.value) not in TERMINAL_STATUSES:
        return 0
    if campaigns_dir is None:
        from haa.config import _PROJECT_ROOT
        campaigns_dir = _PROJECT_ROOT / "data" / "campaigns"

    archive_path = Path(campaigns_dir) / campaign_id / "events_archive.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    events = store.list_events(campaign_id)
    if not events:
        return 0
    data = [e.model_dump(mode="json") for e in events]
    archive_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    deleted = store.delete_campaign_events(campaign_id)
    logger.info("rotated %d events for %s → %s", deleted, campaign_id,
                archive_path)
    return deleted


def archive_stats(store) -> dict[str, Any]:
    return {"events_main": len(store.list_events())}
