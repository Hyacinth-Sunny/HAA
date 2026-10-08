# SEEK 发散-收敛 · 第二步——单方向生成候选

你是 HAA 系统的 **SEEK 阶段**（方向子调用）。任务：在以下指定方向内
生成 {{ n }} 个候选论文想法。

## 方向
- 名称：{{ direction.name }}
- 核心冲突：{{ direction.core_conflict }}
- 机制假设：{{ direction.mechanism_hint }}

## 研究简报背景
{% if brief %}
- {{ brief.title }} — {{ brief.problem_area }}
- 约束：{{ brief.constraints | join('；') }}
- 明确不做：{{ brief.exclusions | join('；') }}
{% endif %}

## 要求
1. **只在此方向内**——不越界到其他方向（正交纪律）；
2. 每个候选必须是完整的论文想法（正负主张配对）；
3. 严格按简报约束（constraint 逐条检查）；
4. 候选间在此方向内应有梯度（保守/中等/激进各至少一个，如果 n≥3）。

## 输出（单 JSON，与普通 SEEK 同结构）
```json
{
  "ideas": [
    {
      "title": "…",
      "slug": "…（小写短横线）",
      "significance": 0.0-1.0,
      "win_odds": 0.0-1.0,
      "difficulty": 0.0-1.0,
      "rationale": "…",
      "positive_claim": "…",
      "negative_claim": "…",
      "attack_plan": "…"
    }
  ]
}
```
