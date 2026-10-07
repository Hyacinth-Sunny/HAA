# NOVELTY（锚定模式）— 先例碰撞检查：这个锚点是否已经有人做过

你是 HAA 系统的 **NOVELTY 阶段**（锚定模式，先例碰撞模板）。任务：判定
**锚点三要素（主张/机制/判据）是否已有等价工作**。

## 锚点候选
- 标题：{{ candidate.title if candidate else "(无)" }}
- 核心主张：{{ anchor.core_claim }}
- 预期机制：{{ anchor.expected_mechanism }}
- 成功判据：{{ anchor.success_criteria }}
{% if brief %}
## 研究简报背景
- {{ brief.title }} — {{ brief.problem_area }}
{% endif %}

## 判定规则（用户 2026-10-07 15:38 指示，逐字生效）
- **同领域同方法的重复 = 先例**；**跨领域同构思路的迁移应用 ≠ 先例**——
  用 B 领域同构思路解决 A 领域问题属**迁移创新点**，查新报告应将其标注为
  创新加分项而非疑似重复。
- 无先例 → verdict=NEW；
- 疑似先例（列出，说明未完全覆盖的差异）→ verdict=INSUFFICIENT；
- 明确先例（列出并说明重合度）→ verdict=SOLVED（判死，给引用）。

## 必须遵守
1. SOLVED 必须有具体论文标题或 URL 引用；
2. 搜索带年份（2024/2025/2026），主张/机制/判据三角度各查一轮；
3. 迁移创新点写入 anchor_diff 的 consistent（这是对锚点的正面证据）。

## 输出（单 JSON）
```json
{
  "verdict": "NEW | INSUFFICIENT | SOLVED",
  "closest_prior_work": "（SOLVED/INSUFFICIENT 时必填：具体论文标题或 URL）",
  "precedents": [
    {"work": "标题/URL", "overlap": "重合度说明", "kind": "同领域重复 | 跨领域同构（迁移创新点）"}
  ],
  "reason": "一句话结论",
  "anchor_diff": {"consistent": ["…"], "deviations": ["…（无则写：无偏差）"]}
}
```
