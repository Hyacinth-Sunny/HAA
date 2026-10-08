"""评审三件套（大修第三章 §12，批次12 核心交付——补齐件）。

1. **智慧库注入**：REVIEW 各路 prompt 在楼层 200+ 注入 wisdom 切片
   （按候选的论文分型匹配 data/review_wisdom/ 下的冷启动文件）。
2. **第五路异家族外部评审**：litellm 接另一家族模型独立评审。
3. **区分度触发的标准修订**：同型候选评分极差/标准差低于阈值时，
   先重写评分细则再审一次（触发式，上限 2 次/campaign）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("haa.review_trio")

from haa.config import _PROJECT_ROOT
WISDOM_DIR = _PROJECT_ROOT / "data" / "review_wisdom"

# 区分度阈值（§12 第 3 条：极差 < 0.5 个等级单位触发修订）
DISTINCTION_RANGE_THRESHOLD = 0.5
DISTINCTION_STD_THRESHOLD = 0.05
MAX_REVISIONS_PER_CAMPAIGN = 2


# ==================== 1. 智慧库注入 ====================

def load_wisdom(lens: str, paper_type: str) -> dict[str, Any] | None:
    """按 lens+分型加载智慧库切片。"""
    path = WISDOM_DIR / f"{lens}-{paper_type}.md"
    if not path.exists():
        # 退化到通用（external-general 或 lens-general）
        path = WISDOM_DIR / f"{lens}-general.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        # 解析 front-matter（YAML 在 # 标题行后面，我们用 --- 分隔）
        # 格式：# 标题\n\nkey: value\n...
        lines = text.split("\n")
        data: dict[str, Any] = {}
        current_key = None
        current_list: list = []
        for line in lines[1:]:  # skip title
            stripped = line.strip()
            if stripped.startswith("- "):
                if current_key:
                    current_list.append(stripped[2:])
            elif ":" in stripped and not stripped.startswith("#"):
                if current_key and current_list:
                    data[current_key] = current_list
                    current_list = []
                key, _, val = stripped.partition(":")
                current_key = key.strip()
                val = val.strip()
                if val:
                    data[current_key] = val
                    current_key = None
            elif stripped == "" and current_key and current_list:
                data[current_key] = current_list
                current_list = []
                current_key = None
        if current_key and current_list:
            data[current_key] = current_list
        return data if data else None
    except Exception as exc:
        logger.warning("wisdom load failed (%s): %s", path.name, exc)
        return None


def render_wisdom_injection(lens: str, paper_type: str) -> str:
    """智慧库切片的 prompt 注入段（楼层 200+ 语义）。"""
    wisdom = load_wisdom(lens, paper_type)
    if not wisdom:
        return ""
    parts = ["⛓⛓⛓ 评审智慧库注入（校准锚+常见批评模式）⛓⛓⛓"]
    rubric = wisdom.get("rubric_patterns") or []
    if rubric:
        parts.append("**评分要点（从评审智慧提炼）**：")
        parts += [f"  - {r}" for r in rubric[:5]]
    critiques = wisdom.get("common_critiques") or []
    if critiques:
        parts.append("**常见批评（同类论文最常被抨击的点）**：")
        parts += [f"  - {c}" for c in critiques[:5]]
    calib = wisdom.get("calibration_examples") or []
    if calib:
        parts.append("**校准锚（对照校准你的分数）**：")
        if isinstance(calib, list):
            for c in calib[:3]:
                if isinstance(c, str) and "score:" in c:
                    parts.append(f"  {c}")
                elif isinstance(c, dict):
                    parts.append(f"  score={c.get('score','?')}: {c.get('reason','')}")
    return "\n".join(parts)


# ==================== 2. 第五路异家族外部评审 ====================

def run_external_review(paper_text: str, *, config=None,
                         brief_block: str = "") -> dict[str, Any]:
    """第五路：接异家族模型独立评审。

    模型路由：``llm.sub_agent_model``（或专用 ``review.external_model``
    配置——与生成论文的模型**不同家族**是唯一硬要求）。
    """
    external_model = ""
    if config is not None:
        external_model = str(
            getattr(getattr(config, "llm", None), "sub_agent_model", "") or "")
    if not external_model:
        # 无异家族模型配置时降级：返回"跳过"标记（不崩——外部评审是
        # 增强件，缺席不影响四路盲审的既有判定）
        return {"score": None, "verdict": "(skipped: no external model)",
                "skipped": True}

    from haa.prompts import render_prompt
    system = render_prompt("review/external", brief_block=brief_block)
    # 经 litellm 直调异家族
    try:
        import litellm
        resp = litellm.completion(
            model=external_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": paper_text[:60000]},
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        text = resp.choices[0].message.content or ""
        # 提取 JSON
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {"score": data.get("score"),
                    "verdict": data.get("verdict", ""),
                    "major_issues": data.get("major_issues", []),
                    "skipped": False}
        return {"score": None, "verdict": text[:500], "skipped": False}
    except Exception as exc:
        logger.warning("external review failed: %s", exc)
        return {"score": None, "verdict": f"(error: {exc})", "skipped": True}


# ==================== 3. 区分度触发的标准修订 ====================

def needs_standard_revision(scores: list[float]) -> bool:
    """区分度检测：极差 < 0.5 或标准差 < 0.05 → 触发修订。"""
    if len(scores) < 2:
        return False
    range_ = max(scores) - min(scores)
    mean = sum(scores) / len(scores)
    std = (sum((s - mean) ** 2 for s in scores) / len(scores)) ** 0.5
    return range_ < DISTINCTION_RANGE_THRESHOLD or std < DISTINCTION_STD_THRESHOLD


def render_revision_prompt(scores: list[float], paper_type: str) -> str:
    """标准修订提示词：先重写评分细则，再审一次。"""
    wisdom = load_wisdom("standard-revision", paper_type) or {}
    triggers = wisdom.get("trigger", "")
    return f"""## ⚠ 评审标准修订指令（区分度不足触发）

上一轮评审的 {len(scores)} 个同型候选得分：
{', '.join(f'{s:.2f}' for s in scores)}

极差 = {max(scores) - min(scores):.2f}（阈值 {DISTINCTION_RANGE_THRESHOLD}）
标准差 = {(sum((s - sum(scores)/len(scores))**2 for s in scores)/len(scores))**0.5:.3f}（阈值 {DISTINCTION_STD_THRESHOLD}）

**修订要求**：
1. 先诊断为什么上一轮区分不开（标准过松/维度缺失/校准漂移）
2. 重写评分细则：明确"什么才算达标"的**正例**和"什么不算"的**负例**
3. 增加至少一个此前被忽略的评审维度
4. 用修订后的标准重新评审（给出新的分数）

输出 JSON：{{"diagnosis": "...", "revised_criteria": "...", "scores": [...]}}
"""


# ==================== 可视化纪律参数化（第五章 §5 补齐） ====================

FIGURE_MAX_RETRIES = 2  # 视觉核验重试上限
FIGURE_GIVE_UP_NO_MORBID = True  # 多次不过→放弃该图，不触发濒死


def generate_chart_with_discipline(code: str, output_path: str,
                                   expected_description: str,
                                   *, config=None, cwd: str | None = None
                                   ) -> dict[str, Any]:
    """图表纪律闭环（§5）：生成→视觉核验→重试(≤2)→仍不过→放弃+记失败清单。"""
    from haa.tools.visualization import generate_chart, verify_chart

    failure_list: list[str] = []
    for attempt in range(1, FIGURE_MAX_RETRIES + 1):
        gen = generate_chart(code, output_path, cwd=cwd)
        if not gen.get("ok"):
            failure_list.append(f"attempt {attempt}: generation failed: "
                                f"{gen.get('error', '')[:200]}")
            continue
        verify = verify_chart(output_path, expected_description, config)
        if verify.get("verified"):
            return {"ok": True, "attempts": attempt,
                    "description": verify.get("description", "")}
        failure_list.append(f"attempt {attempt}: visual verify failed: "
                            f"{verify.get('issues', '')[:200]}")

    # 全部重试失败 → 放弃该图（§5 纪律：不触发濒死、失败清单呈报）
    return {"ok": False, "attempts": FIGURE_MAX_RETRIES,
            "failure_list": failure_list,
            "note": "figure abandoned after max retries — "
                    "paper will use text description instead (no moribund)"}


# ==================== 编译循环纪律（第五章 §4 补齐） ====================

COMPILE_MAX_ROUNDS = 5


def compile_with_discipline(paper_dir: str | Path, *,
                            max_rounds: int = COMPILE_MAX_ROUNDS
                            ) -> dict[str, Any]:
    """编译循环纪律（§4）：编译→读错→修→再编译→…→上限5→带已知错误交付。

    返回 {ok, rounds, known_errors, pdf_path}。
    超限不崩溃——输出"带已知编译错误的交付候选"并标注错误清单。
    """
    from haa.p3.latex_compiler import LatexCompiler
    compiler = LatexCompiler()
    known_errors: list[str] = []

    for round_num in range(1, max_rounds + 1):
        result = compiler.compile(paper_dir)
        if result.ok:
            return {"ok": True, "rounds": round_num,
                    "known_errors": [], "pdf_path": str(result.pdf_path)}
        # 提取本轮错误
        errors = _extract_latex_errors(result)
        known_errors = errors
        logger.info("compile round %d/%d: %d error(s)", round_num, max_rounds,
                    len(errors))
        if round_num >= max_rounds:
            break
        # 修错循环由调用方（P3 agent loop）驱动——此处返回错误供 edit_file
        # （编译循环需要模型介入修错，纯代码只做编译+错误提取）

    # 超限 → 带已知错误交付候选（§4：不静默、不濒死）
    return {"ok": False, "rounds": max_rounds,
            "known_errors": known_errors,
            "note": f"delivered with {len(known_errors)} known compilation "
                    f"error(s) after {max_rounds} rounds — user adjudicates",
            "pdf_path": _check_pdf(paper_dir)}


def _extract_latex_errors(result) -> list[str]:
    """从编译结果提取结构化错误（行号+类型+上下文）。"""
    errors: list[str] = []
    log = getattr(result, "log", "") or ""
    import re
    for m in re.finditer(r"^!(.*?)(?:\n|$)", log, re.M):
        errors.append(m.group(1).strip())
    return errors[:10]


def _check_pdf(paper_dir) -> str:
    """检查是否有部分生成的 PDF。"""
    p = Path(paper_dir) / "main.pdf"
    return str(p) if p.exists() else ""
