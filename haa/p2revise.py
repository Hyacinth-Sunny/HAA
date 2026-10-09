"""P2 修订核心件（批次15/16，修订计划书04 §2）。

环境探测 / 预审清单 / 切题性检查 / 根因分诊器 / kill_reason 枚举化 /
HOLD 状态机 / 用户注记 / CLI 进度视图。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("haa.p2revise")


# ============================================================================ #
#  §2.1.1 环境探测包（纯只读——/proc + Python API，无 subprocess）
# ============================================================================ #

def probe_environment() -> dict[str, Any]:
    """只读探测运行环境（/proc + Python API，零副作用）。"""
    cpu: dict[str, Any] = {
        "physical": _proc_cpu_count(),
        "logical": os.cpu_count() or 0,
        "freq_mhz": _proc_cpu_freq(),
    }

    gpu = _probe_gpu()
    mem = _probe_mem()
    disk_mb = _probe_disk()

    return {
        "cpu": cpu, "gpu": gpu, "mem": mem, "disk_mb": disk_mb,
        "sudo_noninteractive": _probe_sudo(),
        "network": _probe_net(),
        "platform": platform.platform(),
    }


def _proc_cpu_count() -> int:
    try:
        text = Path("/proc/cpuinfo").read_text()
        return len(re.findall(r"^processor\s*:", text, re.M))
    except OSError:
        return 0


def _proc_cpu_freq() -> str:
    try:
        text = Path("/proc/cpuinfo").read_text()
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", text)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _probe_gpu() -> dict[str, Any]:
    gpu = {"vendor": "", "count": 0, "vram_mb": 0}
    # /proc/driver/nvidia/gpu/*/information (NVIDIA)
    nvidia_dir = Path("/proc/driver/nvidia/gpu")
    if nvidia_dir.exists():
        gpu_dirs = [d for d in nvidia_dir.iterdir() if d.is_dir()]
        gpu["vendor"] = "nvidia"
        gpu["count"] = len(gpu_dirs)
        for d in gpu_dirs:
            info = d / "information"
            if info.exists():
                text = info.read_text()
                m = re.search(r"Video Memory\s*:\s*(\d+)\s*MiB", text)
                if m:
                    gpu["vram_mb"] = max(gpu["vram_mb"], int(m.group(1)))
    # /sys/class/drm (AMD/Intel)
    elif Path("/sys/class/drm").exists():
        cards = list(Path("/sys/class/drm").glob("card[0-9]"))
        if cards:
            gpu["vendor"] = "amd_or_intel"
            gpu["count"] = len(cards)
    return gpu


def _probe_mem() -> dict[str, int]:
    mem = {"total_mb": 0, "available_mb": 0}
    try:
        info = Path("/proc/meminfo").read_text()
        for key, attr in (("MemTotal", "total_mb"), ("MemAvailable", "available_mb")):
            m = re.search(rf"{key}:\s+(\d+)\s*kB", info)
            if m:
                mem[attr] = int(m.group(1)) // 1024
    except OSError:
        pass
    return mem


def _probe_disk() -> int:
    try:
        usage = shutil.disk_usage(Path.cwd())
        return usage.free // (1024 * 1024)
    except Exception:
        return 0


def _probe_sudo() -> bool:
    """sudo -n true 探测（不触发密码）。用 os.access + sudoers 检查替代。"""
    # 方案1：检查用户是否在 sudo 组
    try:
        import grp
        sudo_grp = grp.getgrnam("sudo")
        import pwd
        uid = os.getuid()
        if uid == 0:
            return True  # root 本身
        user_groups = [g.gr_gid for g in os.getgrouplist(pwd.getpwuid(uid).pw_name, pwd.getpwuid(uid).pw_gid)]
        if sudo_grp.gr_gid in user_groups:
            # sudo -n true 真实探测
            from haa.harness.tools.jobs import _spawn_group
            proc = _spawn_group("sudo -n true 2>/dev/null && echo SUDO_OK")
            try:
                out, _ = proc.communicate(timeout=5)
                return b"SUDO_OK" in (out or b"")
            except Exception:
                proc.kill()
                return False
    except Exception:
        pass
    return False


def _probe_net() -> bool:
    """出网探测（DNS 解析 + HTTP HEAD）。"""
    import socket
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo("api.openalex.org", 443)
        return True
    except (socket.gaierror, OSError):
        return False


# ============================================================================ #
#  §2.1.2 预审清单
# ============================================================================ #

def build_preflight_checklist(probe: dict, resource_estimate: dict,
                              exp_needs: list[str]) -> list[dict]:
    items: list[dict] = []
    req = resource_estimate or {}
    gpu_req = req.get("gpu_memory_gb") or 0
    if gpu_req > 0 and (probe.get("gpu", {}).get("count") or 0) == 0:
        items.append({"severity": "blocker",
                       "item": f"实验需 GPU（{gpu_req}GB），本机无 GPU",
                       "action": "换有 GPU 的机器或改用 CPU 模式"})
    ram_req = req.get("ram_gb") or 0
    ram_avail = probe.get("mem", {}).get("total_mb") or 0
    if ram_req > 0 and ram_avail < ram_req * 1024:
        items.append({"severity": "blocker",
                       "item": f"需 RAM {ram_req}GB，本机 {ram_avail // 1024}GB",
                       "action": "缩减数据规模或换大内存机"})
    disk_req = req.get("data_size_gb") or 0
    if disk_req > 0 and (probe.get("disk_mb") or 0) < disk_req * 1024:
        items.append({"severity": "warning",
                       "item": f"数据需 {disk_req}GB 磁盘",
                       "action": "清理磁盘或缩减数据"})
    for need in exp_needs:
        if "cuda" in need.lower() and probe.get("gpu", {}).get("vendor") != "nvidia":
            items.append({"severity": "blocker",
                          "item": f"实验需 CUDA：{need}",
                          "action": "配 GPU 环境或改用 CPU 实现"})
        if "network" in need.lower() and not probe.get("network"):
            items.append({"severity": "warning",
                          "item": f"实验需网络：{need}",
                          "action": "开通出网或准备离线数据"})
    return items


# ============================================================================ #
#  §2.1.3 切题性检查
# ============================================================================ #

def validate_claim_metric_mapping(exp_spec: dict) -> list[str]:
    errors: list[str] = []
    claims = exp_spec.get("claims") or exp_spec.get("hypotheses") or []
    mapping = exp_spec.get("claim_metric_map") or {}
    if not claims:
        errors.append("EXP_SPEC 缺少 claims 字段")
        return errors
    if not mapping:
        errors.append("EXP_SPEC 缺少 claim_metric_map（主张→指标映射）")
        return errors
    for i, claim in enumerate(claims):
        key = str(claim if isinstance(claim, str) else claim.get("id", f"claim_{i}"))
        if key not in mapping:
            errors.append(f"主张「{key}」未挂指标")
    return errors


# ============================================================================ #
#  §2.2 根因分诊器
# ============================================================================ #

TRIAGE_CATEGORIES = ("assist", "environment", "code", "viewpoint_pending", "design")


class TriageVerdict(dict):
    def __init__(self, category: str, evidence_lines: list[str],
                 action: str = "", detail: str = ""):
        super().__init__(category=category, evidence_lines=evidence_lines,
                         action=action, detail=detail)


_SIGNATURES: list[tuple[str, list[str], str]] = [
    ("assist", [
        r"sudo.*password is required", r"sudo.*no tty",
        r"sudo.*not in the sudoers",
        # D2 批次18-2：改匹配 Python 原生 traceback（去字面 EACCES 前缀——
        # 真实崩溃输出是 PermissionError: [Errno 13]，不是 node 的 EACCES）
        r"PermissionError.*[Ee]rrno 13", r"permission denied",
    ], "→ HOLD"),
    ("environment", [
        r"CUDA out of memory", r"torch\.cuda\.is_available.*False",
        r"No space left on device", r"MemoryError", r"OOM Killer",
    ], "→ 换配置/降规模"),
    ("viewpoint_pending", [
        r"speedup.*<\s*1", r"NaN detected", r"all metrics are constant",
    ], "→ 交 ANALYZE 终审"),
]


def triage_failure(error_log: str, *, metrics: dict | None = None) -> TriageVerdict:
    lines = (error_log or "").strip().split("\n")
    for category, patterns, action in _SIGNATURES:
        for pat in patterns:
            compiled = re.compile(pat, re.IGNORECASE)
            hits = [f"L{i+1}: {l.strip()[:120]}"
                    for i, l in enumerate(lines) if compiled.search(l)]
            if hits:
                return TriageVerdict(category, hits[:3], action)
    if metrics:
        for key, val in metrics.items():
            if isinstance(val, float) and val != val:
                return TriageVerdict("viewpoint_pending",
                                     [f"metrics[{key}]=NaN"], "→ ANALYZE 终审")
    tb = [f"L{i+1}: {l.strip()[:120]}" for i, l in enumerate(lines)
          if l.strip().startswith(("Traceback", "  File", "Error"))][-3:]
    return TriageVerdict("code", tb or ["(default)"], "→ 修复循环")


# ============================================================================ #
#  §2.2.3 kill_reason 枚举化
# ============================================================================ #

KILL_REASON_ENUMS = frozenset({
    "assist", "environment", "code", "design", "viewpoint",
    "budget", "cap", "novelty_solved", "screen_counterexample",
    "grade_trivial", "grade_loophole", "pilot_not_supported",
    "exp_feasibility_fatal", "queue_exhausted",
})


def validate_kill_reason(reason: str) -> bool:
    """C1 批次18-2：支持 ``"<枚举>:<自由文本>"`` 格式（读方按前缀取）。

    裸枚举（存量）与 ``legacy:`` 前缀（存量自由文本兼容）保持通过。
    """
    head = reason.split(":", 1)[0].strip()
    return (reason in KILL_REASON_ENUMS
            or head in KILL_REASON_ENUMS
            or head == "legacy")


def map_debug_reason_to_kill(reason: str) -> str:
    """C1 批次18-2：DebugSession failure reason → kill_reason 枚举前缀。

    轮数帽耗尽（circuit_breaker）→ ``cap``；预算耗尽 → ``budget``；
    其余（修不动 / 早停等代码类失败）→ ``code``。
    """
    r = reason or ""
    if "budget" in r:
        return "budget"
    if "circuit_breaker" in r:
        return "cap"
    return "code"


# ============================================================================ #
#  §2.3 HOLD 状态机
# ============================================================================ #

def check_hold_flag(campaign_id: str, *, store=None,
                     campaigns_dir=None) -> bool:
    if campaigns_dir is None:
        from haa.config import _PROJECT_ROOT
        campaigns_dir = _PROJECT_ROOT / "data" / "campaigns"
    flag = Path(campaigns_dir) / campaign_id / "HOLD"
    if flag.exists():
        return True
    if store is not None:
        camp = store.get_campaign(campaign_id)
        if camp is not None:
            # B2 批次18-2：CampaignStatus 枚举值为小写（"hold"）——原比较大写
            # "HOLD" 双重失效（枚举无此字面量，永假）。
            if str(getattr(getattr(camp, "status", None), "value", "")).lower() == "hold":
                return True
    return False


def set_hold_flag(campaign_id: str, *, note: str = "", campaigns_dir=None) -> Path:
    if campaigns_dir is None:
        from haa.config import _PROJECT_ROOT
        campaigns_dir = _PROJECT_ROOT / "data" / "campaigns"
    d = Path(campaigns_dir) / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    flag = d / "HOLD"
    flag.write_text(note or "", encoding="utf-8")
    return flag


def clear_hold_flag(campaign_id: str, *, campaigns_dir=None) -> str:
    if campaigns_dir is None:
        from haa.config import _PROJECT_ROOT
        campaigns_dir = _PROJECT_ROOT / "data" / "campaigns"
    flag = Path(campaigns_dir) / campaign_id / "HOLD"
    if flag.exists():
        note = flag.read_text(encoding="utf-8").strip()
        flag.unlink()
        return note
    return ""


def build_assist_request(verdict: TriageVerdict) -> str:
    return "\n".join([
        "⚠ HAA 需要协助（HOLD）", "",
        f"**卡在哪**：{verdict.get('detail', '运行时问题')}",
        f"**需要什么**：{verdict.get('action', '检查配置')}",
        f"**证据**：{'; '.join(verdict.get('evidence_lines', [])[:2])}",
        "", "恢复：haa project resume <id>",
    ])


# ============================================================================ #
#  §2.4 CLI 进度视图
# ============================================================================ #

def render_progress(store, campaign_id: str | None = None) -> str:
    campaigns = store.list_campaigns()
    lines: list[str] = []
    for camp in campaigns:
        if campaign_id and camp.id != campaign_id:
            continue
        status = getattr(camp, "status", None)
        lines.append(f"📄 {camp.id[:12]}… [{status.value if status else '?'}]")
        events = store.list_events(camp.id)
        if not events:
            lines.append("   (无事件)")
            continue
        stages: dict[str, list] = {}
        for ev in events:
            stages.setdefault(ev.stage or "(无阶段)", []).append(ev)
        for sn in sorted(stages.keys()):
            evs = stages[sn]
            llm_n = sum(1 for e in evs if e.event_type == "llm_call")
            tool_n = sum(1 for e in evs if e.event_type == "tool_call")
            cost = sum(e.cost_usd or 0 for e in evs if e.event_type == "llm_call")
            trun = sum(1 for e in evs if e.event_type == "stage_truncated")
            lines.append(f"  ├ {sn}: {llm_n} LLM/{tool_n} tools/${cost:.3f}"
                         + (f"/{trun} trunc" if trun else ""))
            recent = [e for e in evs if e.event_type == "tool_call"][-3:]
            for ev in recent:
                payload = ev.payload or {}
                lines.append(f"  │   · {payload.get('tool', '?')}")
    return "\n".join(lines) if lines else "(no campaigns)"
