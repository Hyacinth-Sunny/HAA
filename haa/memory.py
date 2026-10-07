"""MemoryStore —— 跨项目持久记忆（v1.0）。

形式仿 AutoSci ΩmegaWiki（Markdown 实体 + frontmatter + 派生视图），但引擎
自建轻量版（~250 行）且校验用 pydantic（比它的 lint 更硬）。只移植两样核心：

1. **墓志铭**：每个死掉的 idea 留一页（failure_reason + kill_stage +
   kill_evidence）——下次 SEEK 前注入，防重复踩坑。
2. **派生双纸**：context_brief.md（全库压缩上下文）+ open_questions.md
   （未解问题清单）——引擎重编，人可直接读。

设计铁律（v1.0 落实）：
- **只注入墓碑，绝不注入成功模式**（模式侧留给 v1.1 且仅进修复端）
- 读写失败永不影响主管线（记忆是附加层）

目录布局::

    data/memory/
      ├── ideas/<slug>.md        # 结构化记忆页（frontmatter + 正文）
      ├── concepts/<slug>.md     # 领域概念页（v1.0 最小版，预留）
      ├── context_brief.md       # 派生：全库统计 + 按域失败摘要
      ├── open_questions.md      # 派生：未死透的方向（proposed/tested）
      └── log.md                 # 只追加操作日志
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("haa.memory")

_STOPWORDS = {
    "the", "a", "an", "for", "with", "and", "or", "of", "in", "on", "to",
    "via", "using", "based", "from", "by", "is", "are", "as", "that", "this",
}


# --------------------------------------------------------------------------- #
#  实体模型
# --------------------------------------------------------------------------- #

class MemoryIdea(BaseModel):
    """一张 idea 记忆页（墓志铭或存活记录）。"""

    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    status: str = Field(
        description="proposed | tested | validated | failed | published"
    )
    origin_project: str = ""
    origin_brief_title: str = ""
    domain_keywords: list[str] = Field(default_factory=list)
    core_claim: str = Field(
        default="",
        description="核心主张（大修批次2：campaign 转写时自 kill 记录携带，供跨 campaign 死路匹配升级到语义位）",
    )
    failure_reason: str = ""
    kill_stage: str = ""
    kill_evidence: str = ""
    grade: str = ""
    date: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    @field_validator("status")
    @classmethod
    def _status_values(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("proposed", "tested", "validated", "failed", "published"):
            raise ValueError(f"invalid status {v!r}")
        return v

    @model_validator(mode="after")
    def _failed_needs_reason(self) -> "MemoryIdea":
        # status=failed 时 failure_reason 必填（墓志铭机制的核心约束）。
        # model_validator：field_validator 对默认值不触发（pydantic v2）。
        if self.status == "failed" and not self.failure_reason.strip():
            raise ValueError("failure_reason is required when status=failed")
        return self

    def frontmatter(self) -> str:
        lines = ["---"]
        for key, value in self.model_dump().items():
            if isinstance(value, list):
                lines.append(f"{key}: [{', '.join(value)}]")
            else:
                text = str(value).replace('"', "'")
                lines.append(f'{key}: "{text}"' if text else f"{key}: \"\"")
        lines.append("---")
        return "\n".join(lines)

    def to_page(self) -> str:
        body = [self.frontmatter(), "", f"# {self.title}", ""]
        if self.core_claim:
            body += ["**核心主张**：" + self.core_claim, ""]
        if self.failure_reason:
            body += [f"**死因（{self.kill_stage or '?'}）**：{self.failure_reason}", ""]
        if self.kill_evidence:
            body += ["**证据**：" + self.kill_evidence, ""]
        return "\n".join(body)


_FM_LINE = re.compile(r'^(\w+):\s*(.*)$')


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML-flat frontmatter parser (lists as [a, b], strings quoted or not)."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()[1:]
    out: dict[str, Any] = {}
    for ln in lines:
        if ln.strip() == "---":
            break
        m = _FM_LINE.match(ln)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            out[key] = [p.strip().strip('"') for p in inner.split(",")] if inner else []
        else:
            out[key] = raw.strip('"')
    return out


def _load_idea(path: Path) -> MemoryIdea | None:
    try:
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        return MemoryIdea.model_validate(fm)
    except (ValidationError, OSError, ValueError) as exc:
        logger.warning("skip malformed memory page %s: %s", path.name, exc)
        return None


# --------------------------------------------------------------------------- #
#  MemoryStore
# --------------------------------------------------------------------------- #

class MemoryStore:
    """跨项目记忆库：写页 + 派生视图重编 + 按简报检索墓碑。

    所有公开方法吞掉自身异常（log 后继续）——记忆层绝不影响主管线。
    """

    def __init__(self, root: str | Path, *, max_inject: int = 8) -> None:
        self.root = Path(root)
        self.ideas_dir = self.root / "ideas"
        self.max_inject = max(0, int(max_inject))

    # -- 写侧 ------------------------------------------------------------- #

    def record_idea(self, page: MemoryIdea) -> None:
        try:
            self.ideas_dir.mkdir(parents=True, exist_ok=True)
            path = self.ideas_dir / f"{_safe_name(page.slug)}.md"
            path.write_text(page.to_page(), encoding="utf-8")
            self._append_log(f"record {page.slug} ({page.status})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_idea(%s) failed: %s", page.slug, exc)

    def record_batch(self, pages: list[MemoryIdea]) -> None:
        for p in pages:
            self.record_idea(p)
        if pages:
            self.rebuild_derivations()

    # -- 派生视图 ---------------------------------------------------------- #

    def list_ideas(self) -> list[MemoryIdea]:
        if not self.ideas_dir.is_dir():
            return []
        out = []
        for path in sorted(self.ideas_dir.glob("*.md")):
            idea = _load_idea(path)
            if idea is not None:
                out.append(idea)
        return out

    def rebuild_derivations(self) -> None:
        """重编 context_brief.md + open_questions.md（幂等）。"""
        try:
            ideas = self.list_ideas()
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "context_brief.md").write_text(
                self._render_brief(ideas), encoding="utf-8"
            )
            (self.root / "open_questions.md").write_text(
                self._render_open_questions(ideas), encoding="utf-8"
            )
            self._append_log(f"rebuild derivations ({len(ideas)} ideas)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("rebuild_derivations failed: %s", exc)

    def _render_brief(self, ideas: list[MemoryIdea]) -> str:
        failed = [i for i in ideas if i.status == "failed"]
        live = [i for i in ideas if i.status in ("proposed", "tested")]
        pub = [i for i in ideas if i.status in ("published", "validated")]
        lines = [
            "# Context Brief（记忆库压缩上下文）", "",
            f"- 记录总数：{len(ideas)}（失败 {len(failed)} / 存活中 {len(live)} / 已验证 {len(pub)}）",
            f"- 重编时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
            "## 失败摘要（墓志铭索引）", "",
        ]
        for i in failed:
            lines.append(
                f"- [{i.date}] {i.slug}（{', '.join(i.domain_keywords[:4]) or '—'}）"
                f"死于 {i.kill_stage or '?'}：{(i.failure_reason or '')[:120]}"
            )
        if not failed:
            lines.append("- （无失败记录）")
        return "\n".join(lines) + "\n"

    def _render_open_questions(self, ideas: list[MemoryIdea]) -> str:
        live = [i for i in ideas if i.status in ("proposed", "tested")]
        lines = ["# Open Questions（未死透的方向）", ""]
        for i in live:
            lines.append(
                f"- [{i.status}] {i.title}（{i.date}，{i.origin_brief_title or '?'}）"
            )
        if not live:
            lines.append("- （当前无未决方向）")
        return "\n".join(lines) + "\n"

    # -- 读侧（检索） ------------------------------------------------------- #

    def brief_for(self, brief: Any, top_k: int | None = None) -> str:
        """按简报关键词检索相关墓碑，返回可注入 prompt 的压缩摘要。

        只含墓碑与未解问题（铁律：绝不注入成功模式）。无命中返回空串。
        """
        try:
            keywords = self._brief_keywords(brief)
            failed = self.failed_in_neighborhood(keywords, top_k or self.max_inject)
            # 记忆卫生（v1.0.2）：无死因的墓碑不注入——"死于 ?："对模型是
            # 纯噪声（smoke4 注入的正是这种遗留测试条目）。
            failed = [i for i in failed if (i.failure_reason or "").strip()]
            live = [i for i in self.list_ideas() if i.status in ("proposed", "tested")][:3]
            parts: list[str] = []
            if failed:
                tomb = "\n".join(
                    f"- [{i.date}] {i.slug}（{', '.join(i.domain_keywords[:3]) or '—'}）"
                    f"死于 {i.kill_stage or '?'}：{(i.failure_reason or '')[:140]}"
                    for i in failed
                )
                parts.append(
                    "## 历史记忆（以下方向此前已尝试并失败，请勿重复；失败原因供规避参考）\n"
                    + tomb
                )
            if live:
                oq = "\n".join(f"- {i.title}（{i.status}）" for i in live)
                parts.append("## 未解方向（此前提出但未走完的方向，可参考其切入）\n" + oq)
            return "\n\n".join(parts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brief_for failed: %s", exc)
            return ""

    def failed_in_neighborhood(
        self, keywords: list[str], top_k: int = 8
    ) -> list[MemoryIdea]:
        """关键词重合度检索失败墓碑（词集交集排序，零依赖）。"""
        kws = {w.lower() for w in keywords if w.lower() not in _STOPWORDS}
        if not kws:
            return []
        scored: list[tuple[int, MemoryIdea]] = []
        for idea in self.list_ideas():
            if idea.status != "failed":
                continue
            page_words = {
                w.lower()
                for w in (*idea.domain_keywords, *idea.title.split(),
                          *idea.slug.split("-"))
                if w.lower() not in _STOPWORDS
            }
            overlap = len(kws & page_words)
            if overlap > 0:
                scored.append((overlap, idea))
        scored.sort(key=lambda t: -t[0])
        return [idea for _, idea in scored[: max(0, top_k)]]

    @staticmethod
    def _brief_keywords(brief: Any) -> list[str]:
        words: list[str] = []
        for attr in ("title", "problem_area"):
            text = str(getattr(brief, attr, "") or "")
            words += [w for w in re.split(r"[^A-Za-z0-9一-鿿]+", text) if len(w) > 2]
        words += list(getattr(brief, "constraints", []) or [])
        return words

    # -- 内部 -------------------------------------------------------------- #

    def _append_log(self, message: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / "log.md", "a", encoding="utf-8") as f:
                f.write(f"- {datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("_append_log failed: %s", exc)


def _safe_name(slug: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", slug.lower().strip()) or "unnamed"


# --------------------------------------------------------------------------- #
#  候选 → 记忆页 转写（ProjectController 写侧调用）
# --------------------------------------------------------------------------- #

def idea_pages_from_campaign(
    campaign_id: str,
    candidates: list[dict[str, Any]],
    kills: list[dict[str, Any]],
    *,
    origin_project: str,
    origin_brief_title: str,
) -> list[MemoryIdea]:
    """把 artifacts 的 candidates.json + kills.json 转写为记忆页。

    candidates: 全量候选（含 filtered/dead/published，dict 形式，来自
    artifacts/candidates.json 或 checkpoint context）。
    kills: extra.kills 记录（带 verdict）。
    """
    kill_by_slug: dict[str, dict[str, Any]] = {}
    for k in kills or []:
        slug = k.get("slug")
        if slug:
            kill_by_slug[slug] = k

    pages: list[MemoryIdea] = []
    for c in candidates or []:
        slug = str(c.get("slug") or "").strip()
        if not slug:
            continue
        status_raw = str(c.get("status") or "proposed").lower()
        kill = kill_by_slug.get(slug)
        if status_raw == "published":
            status = "published"
        elif kill is not None or status_raw == "dead":
            status = "failed"
        elif status_raw == "filtered":
            status = "proposed"  # SEEK cap 滤掉：留档但非墓碑
        else:
            status = "proposed"

        verdict = (kill or {}).get("verdict") or {}
        failure_reason = ""
        kill_stage = ""
        kill_evidence = ""
        if kill is not None:
            kill_stage = str(kill.get("stage") or "")
            blockers = verdict.get("blockers") or []
            if blockers:
                first = blockers[0] if isinstance(blockers[0], dict) else {}
                failure_reason = str(first.get("detail") or verdict.get("reason") or "")[:300]
                kill_evidence = str(first.get("evidence") or "")[:300]
            else:
                failure_reason = str(
                    verdict.get("rationale") or verdict.get("evidence")
                    or kill.get("reason") or ""
                )[:300]
                kill_evidence = str(verdict.get("evidence") or "")[:300]
        elif status == "failed":
            failure_reason = "killed（无 kill 记录，详情见项目档案）"
            kill_stage = "unknown"

        grade = str(c.get("grade") or "") or ""
        if grade.startswith("GradeVerdict.") or grade and grade[0].isupper():
            grade = grade.rsplit(".", 1)[-1].lower()

        try:
            pages.append(MemoryIdea(
                slug=slug,
                title=str(c.get("title") or slug)[:200],
                status=status,
                origin_project=origin_project,
                origin_brief_title=origin_brief_title[:200],
                domain_keywords=_keywords_from_title(str(c.get("title") or "")),
                core_claim=str((kill or {}).get("core_claim") or c.get("positive_claim") or "")[:500],
                failure_reason=failure_reason,
                kill_stage=kill_stage,
                kill_evidence=kill_evidence,
                grade=grade,
            ))
        except ValidationError as exc:
            logger.warning("skip candidate %s: %s", slug, exc)
    return pages


def _keywords_from_title(title: str) -> list[str]:
    words = [w for w in re.split(r"[^A-Za-z0-9一-鿿]+", title) if len(w) > 2]
    return [w.lower() for w in words[:8]]


# --------------------------------------------------------------------------- #
#  大修批次2（第二章 §8）：campaign 内即时墓穴
# --------------------------------------------------------------------------- #

def load_campaign_kills(campaigns_dir, campaign_id: str) -> list[dict[str, Any]]:
    """读 campaigns/<cid>/artifacts/kills.json（容错：缺文件/坏 JSON → 空表）。

    SEEK 注入的文件侧数据源（进程内 context.extra 丢失时——如跨进程
    resume——仍能拿到全量死路）。
    """
    try:
        p = Path(campaigns_dir) / campaign_id / "artifacts" / "kills.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as exc:
        logger.warning("load_campaign_kills(%s) failed: %s", campaign_id, exc)
        return []


def campaign_tomb_block(kills: list[dict[str, Any]], max_chars: int = 1500) -> str:
    """把 campaign 内 kill 记录渲染为可注入 SEEK prompt 的死路清单。

    大修计划书第二章 §8（墓穴即时版）＋第三章 §5.3（SEEK 注入）。注入上限
    1K token 的硬顶纪律（第二章 §11：配置可下调不可上调）。字符折算：
    中文 1 字符 ≈ 0.6-1 token（用户/OpenClaw 2026-10-07 指正：初版
    3000 字符折算偏乐观，实际可折出 2-3K token 超顶 2-3 倍）——按
    1500 字符保守起步；**挂账：接入真实分词器实测校准一次后定稿**
    （未校准前本默认只可下调不可上调）。超限保留在前的记录并标注丢弃数。
    空表返回 ""。逻辑归属楼层 50-99（记忆注入区）；M0 楼层化挂账清偿前
    以 prompt 后缀形式注入（与 _memory_brief_suffix 同通道）。
    """
    entries = [k for k in (kills or []) if isinstance(k, dict)]
    if not entries:
        return ""
    lines: list[str] = [
        "## ⚰ 战役内墓穴（本战役此前轮次已死候选——新候选严禁换皮重提同骨架思路；死因供规避）",
    ]
    dropped = 0
    used = len(lines[0])
    for k in entries:
        stage = str(k.get("stage") or "?")
        title = str(k.get("title") or k.get("slug") or "?")
        reason = str(
            k.get("kill_evidence") or k.get("reason")
            or (k.get("verdict") or {}).get("reason") or ""
        )[:200]
        claim = str(k.get("core_claim") or "")[:160]
        parts = [f"- [{stage}] {title}"]
        if claim:
            parts.append("主张：" + claim)
        if reason:
            parts.append("死因：" + reason)
        line = "｜".join(parts)
        if used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if dropped:
        lines.append(f"（另有 {dropped} 条死路记录因注入上限省略）")
    return "\n".join(lines)
