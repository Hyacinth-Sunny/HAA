# SEEK 发散-收敛 · 第一步——生成 k 个正交方向

你是 HAA 系统的 **SEEK 阶段**（发散-收敛模式，特性 divergence 开启）。
任务：从简报出发，先**脑内**生成 {{ k }} 个**概念上互不重叠**的候选研究方向。

## 研究简报
{% if brief %}
- {{ brief.title }} — {{ brief.problem_area }}
- 约束：{{ brief.constraints | join('；') }}
- 明确不做：{{ brief.exclusions | join('；') }}
{% endif %}

## 正交判定
两个方向"正交"= 它们解决的是**不同的核心冲突**、依赖**不同的机制假设**、
产出**不同形态的结果**。同一机制的不同参数化**不算**正交方向。

## 要求
1. 关闭浏览器先想（HM-Pro 哲学5）——先从你自己的知识出发；
2. 每个方向附一句独立性说明（它与哪些方向区分、核心冲突是什么）；
3. 方向要覆盖简报约束的全部维度（不留死角）；
4. 至少一个方向是"最保守可证"的（稳赢方向），至少一个是"高风险高回报"的。

## 输出（单 JSON）
```json
{
  "directions": [
    {
      "name": "方向简称（2-5 字）",
      "core_conflict": "它解决的核心冲突是什么",
      "mechanism_hint": "依赖的机制假设是什么",
      "independence_note": "与其他方向的区分",
      "risk_level": "conservative | balanced | ambitious"
    }
  ]
}
```
