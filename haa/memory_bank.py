"""记忆库实体层（大修批次 M-b，计划书第二章 §6）。

三层记忆的第三层本体：**文件为主、代码校验**——
- 主存储：``data/memory/{ideas,experiments,verdicts,domain_concepts,
  open_questions}/<id>.md``（front-matter＋可选正文），人可读、git 可审计、
  宿主无关；派生索引（SQLite 只读）可随时从主存储全量重建
  （``haa memory rebuild``）——**主从不可倒置：删 SQLite 可重建，删
  Markdown 即损失**。
- 写权限矩阵（``config/memory_writers.yaml``）：写入者→实体→字段，
  矩阵外一律拒绝，不提供绕过接口。
- 边规则：双向链接强制（写入时自动补反向边，lint 校验成对性）。
- lint：必填字段、链接成对、状态转移合法（禁止复活死实体——复活须新建
  实体并链接旧实体）、front-matter 可解析、ID 唯一。

与既有 ``haa/memory.py``（v1.0 跨项目墓碑页）的关系：并行共存于
``data/memory/``（旧页在根、新实体在五子目录）；M-c（结算管理器批次）
时统一接缝。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("haa.memory_bank")

ENTITY_DIRS = ("ideas", "experiments", "verdicts", "domain_concepts", "open_questions")

IDEA_STATUSES = ("killed", "superseded", "graduated")
EXPERIMENT_VERDICTS = ("supported", "partially", "not_supported", "inconclusive")
CONCEPT_STATUSES = ("defined", "assumed", "imported")
OQ_STATUSES = ("open", "picked", "resolved")

# 状态转移表（§6.4：禁止复活死实体——复活须新建实体并链接旧实体）
IDEA_TRANSITIONS: dict[str, frozenset[str]] = {
    "killed": frozenset(), "superseded": frozenset(), "graduated": frozenset(),
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_date(v: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", v or ""):
        raise ValueError(f"date must be YYYY-MM-DD: {v!r}")
    return v


class IdeaEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idea_id: str = Field(description="i-YYYYMMDD-NNNN，全库唯一")
    title: str = Field(min_length=1)
    core_claim: str = Field(min_length=1)
    structure_fingerprint: dict = Field(default_factory=dict)
    source_campaign: str = ""
    status: str = Field(description="killed | superseded | graduated")
    kill_reason: str = ""
    kill_evidence: str = ""
    problem_class: list[str] = Field(default_factory=list, description="主匹配键：问题类别")
    domain: str = Field(default="", description="描述性元数据，非查询主键")
    killed_by: list[str] = Field(default_factory=list, description="反向边：判决 v-… 列表")
    has_experiment: list[str] = Field(default_factory=list, description="反向边：实验 e-… 列表")
    related: list[str] = Field(default_factory=list)
    date: str = Field(default_factory=_today)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in IDEA_STATUSES:
            raise ValueError(f"idea.status must be one of {IDEA_STATUSES}")
        return v

    @field_validator("idea_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.match(r"^i-\d{8}-\d{4}$", v or ""):
            raise ValueError(f"idea_id must be i-YYYYMMDD-NNNN: {v!r}")
        return v

    @model_validator(mode="after")
    def _killed_needs_reason(self) -> "IdeaEntity":
        if self.status == "killed" and not (self.kill_reason or "").strip():
            raise ValueError("kill_reason is required when status=killed")
        return self

    _check_date_v = field_validator("date")(_check_date)


class ExperimentEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exp_id: str
    idea_id: str = Field(min_length=1, description="归属想法（必填）")
    tier: str = Field(description="pilot | full")
    verdict: str = Field(description="supported | partially | not_supported | inconclusive")
    metrics: dict = Field(default_factory=dict)
    env: str = ""
    solve_sh: str = ""
    date: str = Field(default_factory=_today)

    @field_validator("tier")
    @classmethod
    def _tier(cls, v: str) -> str:
        if v not in ("pilot", "full"):
            raise ValueError("tier must be pilot|full")
        return v

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        if v not in EXPERIMENT_VERDICTS:
            raise ValueError(f"verdict must be one of {EXPERIMENT_VERDICTS}")
        return v

    @field_validator("exp_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.match(r"^e-\d{8}-\d{4}$", v or ""):
            raise ValueError(f"exp_id must be e-YYYYMMDD-NNNN: {v!r}")
        return v

    _check_date_v = field_validator("date")(_check_date)


class VerdictEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict_id: str
    subject: dict = Field(description="{type: idea|experiment|paper_section, id}")
    judge: str = Field(description="GRADE|REVIEW:lens|PILOT")
    conclusion: str = Field(min_length=1)
    evidence: str = ""
    kills: list[str] = Field(default_factory=list, description="反向边：被杀 idea 列表")
    date: str = Field(default_factory=_today)

    @field_validator("verdict_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.match(r"^v-\d{8}-\d{4}$", v or ""):
            raise ValueError(f"verdict_id must be v-YYYYMMDD-NNNN: {v!r}")
        return v

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: dict) -> dict:
        if v.get("type") not in ("idea", "experiment", "paper_section") or not v.get("id"):
            raise ValueError("subject must be {type: idea|experiment|paper_section, id}")
        return v

    _check_date_v = field_validator("date")(_check_date)


class DomainConceptEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept_id: str
    name: str = Field(min_length=1)
    math_formulation: str = Field(min_length=1, description="LaTeX 数学表述（必填）")
    code_refs: list[dict] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    status: str = "defined"
    first_seen_campaign: str = ""
    reused_in: list[str] = Field(default_factory=list)
    date: str = Field(default_factory=_today)

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in CONCEPT_STATUSES:
            raise ValueError(f"status must be one of {CONCEPT_STATUSES}")
        return v

    @field_validator("concept_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.match(r"^c-\d{8}-\d{4}$", v or ""):
            raise ValueError(f"concept_id must be c-YYYYMMDD-NNNN: {v!r}")
        return v

    _check_date_v = field_validator("date")(_check_date)


class OpenQuestionEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oq_id: str
    question: str = Field(min_length=1)
    origin: str = ""
    status: str = "open"
    picked_by: list[str] = Field(default_factory=list)
    date: str = Field(default_factory=_today)

    @field_validator("oq_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.match(r"^q-\d{8}-\d{4}$", v or ""):
            raise ValueError(f"oq_id must be q-YYYYMMDD-NNNN: {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in OQ_STATUSES:
            raise ValueError(f"status must be one of {OQ_STATUSES}")
        return v

    _check_date_v = field_validator("date")(_check_date)


ENTITY_MODELS = {
    "ideas": IdeaEntity,
    "experiments": ExperimentEntity,
    "verdicts": VerdictEntity,
    "domain_concepts": DomainConceptEntity,
    "open_questions": OpenQuestionEntity,
}


class WritePermissionError(Exception):
    """写权限矩阵拒绝（不提供绕过）。"""


class MemoryBank:
    """实体库 API：写（经矩阵+自动反向边）/ 读 / lint / 派生索引。"""

    def __init__(self, root: str | Path, *, writers_config: Path | str | None = None):
        self.root = Path(root)
        self.writers = self._load_writers(writers_config)
        self._index_conn: sqlite3.Connection | None = None

    # -- 路径与解析 ---------------------------------------------------------

    def _dir(self, entity: str) -> Path:
        if entity not in ENTITY_DIRS:
            raise ValueError(f"unknown entity dir: {entity}")
        return self.root / entity

    def _page(self, entity: str, entity_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", entity_id)
        return self._dir(entity) / f"{safe}.md"

    @staticmethod
    def _dump_page(model: BaseModel, body: str = "") -> str:
        fm = yaml.safe_dump(
            json.loads(model.model_dump_json()), allow_unicode=True, sort_keys=False
        )
        return f"---\n{fm}---\n\n{body}".rstrip() + "\n"

    @staticmethod
    def _parse_page(text: str) -> tuple[dict, str]:
        if not text.startswith("---"):
            raise ValueError("page must start with front-matter")
        parts = text.split("\n---", 1)
        raw_fm = parts[0][3:].strip()
        body = parts[1].lstrip("\n-") if len(parts) > 1 else ""
        data = yaml.safe_load(raw_fm) or {}
        if not isinstance(data, dict):
            raise ValueError("front-matter must be a mapping")
        return data, body

    def load(self, entity: str, entity_id: str) -> Any | None:
        p = self._page(entity, entity_id)
        if not p.exists():
            return None
        data, body = self._parse_page(p.read_text(encoding="utf-8"))
        model_cls = ENTITY_MODELS[entity]
        return model_cls.model_validate(data)

    # -- 写权限矩阵 ---------------------------------------------------------

    @staticmethod
    def _load_writers(path: Path | str | None) -> dict:
        if path is None:
            from haa.config import _PROJECT_ROOT

            path = _PROJECT_ROOT / "config" / "memory_writers.yaml"
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            return data.get("writers") or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("memory_writers.yaml unreadable (%s) — bank is read-only", exc)
            return {}

    # 矩阵键用单数实体名（计划书 §6.3 口径）；目录名是复数
    MATRIX_KEY = {"ideas": "idea", "experiments": "experiment",
                  "verdicts": "verdict", "domain_concepts": "domain_concept",
                  "open_questions": "open_question"}

    def _authorize(self, writer: str, entity: str, *, create: bool,
                   model: BaseModel | None, existing: BaseModel | None) -> None:
        rule = (self.writers.get(writer) or {}).get(self.MATRIX_KEY.get(entity, entity))
        if not rule:
            raise WritePermissionError(
                f"writer {writer!r} has no grant on {entity} (write-matrix)"
            )
        if create and not rule.get("create"):
            raise WritePermissionError(f"writer {writer!r} cannot create {entity}")
        # 更新权三源：显式 update / create（自建自改）/ 声明了 fields（字段级）
        if not create and not (rule.get("update") or rule.get("create")
                               or rule.get("fields") is not None):
            raise WritePermissionError(f"writer {writer!r} cannot update {entity}")
        allowed = rule.get("fields") or []
        if "*" in allowed:
            return
        # 状态转移合法性：目标状态必须在矩阵 status_to 内（若有声明）
        status_change_ok = False
        if model is not None and existing is not None:
            status_to = rule.get("status_to")
            new_status = getattr(model, "status", None)
            old_status = getattr(existing, "status", None)
            if status_to and new_status != old_status:
                if new_status not in status_to:
                    raise WritePermissionError(
                        f"writer {writer!r} cannot set {entity}.status="
                        f"{new_status!r} (allowed: {status_to})"
                    )
                status_change_ok = True
            changed = {
                k for k, v in model.model_dump().items()
                if v != existing.model_dump().get(k)
            }
            slack = set(allowed) | {"date"}
            if status_change_ok:
                slack.add("status")
            illegal = changed - slack
            if illegal:
                raise WritePermissionError(
                    f"writer {writer!r} cannot mutate fields {sorted(illegal)} "
                    f"on {entity} (allowed: {allowed or '[]'})"
                )

    # -- 写入（含自动反向边） -----------------------------------------------

    EDGE_RULES = {
        # 正向边（写入方持有）→ 反向边（对端实体自动补）
        ("experiments", "idea_id"): ("ideas", "has_experiment"),
        ("verdicts", "kills"): ("ideas", "killed_by"),
    }

    def write(self, entity: str, model: BaseModel, *, writer: str,
              body: str = "") -> BaseModel:
        """经写权限矩阵写入并自动补反向边；返回存储后的实体。"""
        existing = self.load(entity, self._entity_key(model, entity))
        self._authorize(writer, entity, create=existing is None,
                        model=model, existing=existing)
        if existing is not None:
            self._check_no_revival(entity, existing, model)
        path = self._page(entity, self._entity_key(model, entity))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump_page(model, body), encoding="utf-8")
        self._auto_reverse_edges(entity, model, writer)
        return model

    ID_FIELD_BY_DIR = {"ideas": "idea_id", "experiments": "exp_id",
                       "verdicts": "verdict_id", "domain_concepts": "concept_id",
                       "open_questions": "oq_id"}

    @classmethod
    def _entity_key(cls, model: BaseModel, entity: str | None = None) -> str:
        if entity is not None:
            v = getattr(model, cls.ID_FIELD_BY_DIR[entity], None)
            if v:
                return v
        for key in ("idea_id", "exp_id", "verdict_id", "concept_id", "oq_id"):
            v = getattr(model, key, None)
            if v:
                return v
        raise ValueError("entity has no id field")

    def _check_no_revival(self, entity: str, existing: BaseModel, model: BaseModel) -> None:
        if entity == "ideas" and existing.status in IDEA_TRANSITIONS:
            if model.status != existing.status:
                raise ValueError(
                    f"cannot revive dead entity {self._entity_key(model, entity)} "
                    f"({existing.status} → {model.status}); create a NEW entity "
                    f"and link it via 'related'"
                )

    def _auto_reverse_edges(self, entity: str, model: BaseModel, writer: str) -> None:
        """写入时自动补反向边（§6.2；AutoSci xref 规则）。

        experiments.belongs_to(idea) ⟺ idea.has_experiment
        verdicts.kills(idea)         ⟺ idea.killed_by
        """
        if entity == "experiments":
            idea = self.load("ideas", model.idea_id)
            if idea is not None and model.exp_id not in idea.has_experiment:
                idea.has_experiment.append(model.exp_id)
                self._raw_rewrite("ideas", idea)
        elif entity == "verdicts":
            for idea_id in model.kills:
                idea = self.load("ideas", idea_id)
                if idea is not None and model.verdict_id not in idea.killed_by:
                    idea.killed_by.append(model.verdict_id)
                    self._raw_rewrite("ideas", idea)

    def _raw_rewrite(self, entity: str, model: BaseModel) -> None:
        """反向边补写不走矩阵（边是系统完整性义务，不是写入者意志）。"""
        path = self._page(entity, self._entity_key(model, entity))
        if path.exists():
            _, body = self._parse_page(path.read_text(encoding="utf-8"))
        else:
            body = ""
        path.write_text(self._dump_page(model, body), encoding="utf-8")

    # -- lint ----------------------------------------------------------------

    def lint(self) -> list[str]:
        """全库校验：返回问题清单（空=通过）。挂测试套件（CI 强制）。"""
        problems: list[str] = []
        seen_ids: dict[str, str] = {}
        pages: dict[tuple[str, str], Any] = {}
        for entity in ENTITY_DIRS:
            for p in sorted(self._dir(entity).glob("*.md")):
                try:
                    data, _ = self._parse_page(p.read_text(encoding="utf-8"))
                    model = ENTITY_MODELS[entity].model_validate(data)
                except (ValueError, ValidationError, yaml.YAMLError) as exc:
                    problems.append(f"{p.relative_to(self.root)}: {exc}")
                    continue
                key = self._entity_key(model, entity)
                if key in seen_ids:
                    problems.append(f"duplicate id {key}: {p.name} vs {seen_ids[key]}")
                seen_ids[key] = p.name
                pages[(entity, key)] = model
        # 双向链接成对
        for (entity, _), model in pages.items():
            if entity == "experiments":
                idea = pages.get(("ideas", model.idea_id))
                if idea is None:
                    problems.append(
                        f"experiment {model.exp_id} belongs_to missing idea {model.idea_id}")
                elif model.exp_id not in idea.has_experiment:
                    problems.append(
                        f"edge pair broken: idea {model.idea_id}.has_experiment "
                        f"missing {model.exp_id}")
            if entity == "verdicts":
                for idea_id in model.kills:
                    idea = pages.get(("ideas", idea_id))
                    if idea is None:
                        problems.append(
                            f"verdict {model.verdict_id} kills missing idea {idea_id}")
                    elif model.verdict_id not in idea.killed_by:
                        problems.append(
                            f"edge pair broken: idea {idea_id}.killed_by "
                            f"missing {model.verdict_id}")
        for (entity, _), model in pages.items():
            if entity == "ideas":
                for exp_id in model.has_experiment:
                    if ("experiments", exp_id) not in pages:
                        problems.append(
                            f"idea {model.idea_id}.has_experiment dangling {exp_id}")
                for v_id in model.killed_by:
                    if ("verdicts", v_id) not in pages:
                        problems.append(
                            f"idea {model.idea_id}.killed_by dangling {v_id}")
        return problems

    # -- 派生索引（只读 SQLite，可随时重建） ---------------------------------

    def index_path(self) -> Path:
        return self.root / "index.sqlite"

    def rebuild_index(self) -> int:
        """从 Markdown 主存储全量重建派生索引（`haa memory rebuild`）。"""
        conn = sqlite3.connect(self.index_path())
        try:
            conn.executescript(
                "DROP TABLE IF EXISTS entities;"
                "CREATE TABLE entities ("
                " entity TEXT, entity_id TEXT PRIMARY KEY, payload TEXT, date TEXT);"
            )
            n = 0
            for entity in ENTITY_DIRS:
                for p in sorted(self._dir(entity).glob("*.md")):
                    try:
                        data, _ = self._parse_page(p.read_text(encoding="utf-8"))
                        ENTITY_MODELS[entity].model_validate(data)
                    except Exception:  # noqa: BLE001 — 坏页跳过（lint 报告）
                        continue
                    eid = data.get(cls_id := self.ID_FIELD_BY_DIR.get(entity)) \
                        if False else data.get(self.ID_FIELD_BY_DIR.get(entity))
                    if eid:
                        conn.execute(
                            "INSERT OR REPLACE INTO entities VALUES (?,?,?,?)",
                            (entity, eid, json.dumps(data, ensure_ascii=False),
                             data.get("date", "")),
                        )
                        n += 1
            conn.commit()
            return n
        finally:
            conn.close()

    def query(self, *, problem_class: str | None = None,
              text_like: str | None = None, top_n: int = 20) -> list[dict]:
        """索引查询（memory_query 工具的数据面，M-d 接入）。"""
        if not self.index_path().exists():
            self.rebuild_index()
        conn = sqlite3.connect(self.index_path())
        try:
            rows = conn.execute(
                "SELECT entity, entity_id, payload FROM entities"
            ).fetchall()
        finally:
            conn.close()
        out = []
        needle = (text_like or "").lower()
        for entity, eid, payload in rows:
            data = json.loads(payload)
            if problem_class and problem_class not in (data.get("problem_class") or []):
                continue
            if needle and needle not in payload.lower():
                continue
            out.append({"entity": entity, **data})
            if len(out) >= top_n:
                break
        return out


def next_entity_id(bank_root: Path, prefix: str) -> str:
    """按日期+当日序号发放实体 ID（i/e/v/c/q-YYYYMMDD-NNNN）。"""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    n = 1
    for entity_dir in ENTITY_DIRS:
        d = Path(bank_root) / entity_dir
        if d.exists():
            for p in d.glob(f"{prefix}-{today}-????.md"):
                try:
                    n = max(n, int(p.stem.rsplit("-", 1)[1]) + 1)
                except ValueError:
                    continue
    return f"{prefix}-{today}-{n:04d}"


def idea_id_from_anchor(core_claim: str, expected_mechanism: str,
                        success_criteria: str) -> str:
    """锚点规范化序列化后 SHA-256 前 8 位（第三章 §3 的 idea-ID；锚点不可变）。"""
    normalized = json.dumps(
        [core_claim.strip(), expected_mechanism.strip(), success_criteria.strip()],
        ensure_ascii=False, sort_keys=True,
    )
    return "a" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
