# 评审智慧库：correctness 赛道理论

venue: STOC / FOCS / SODA
paper_type: theory
rubric_patterns:
  - "下界证明必须对 adversaries 的能力给出明确刻画"
  - "算法复杂度分析必须覆盖最坏情况（不能只给平均）"
  - "随机化算法必须给出失败概率的显式界"
common_critiques:
  - "证明中的概率论证不严谨（未处理事件相关性）"
  - "复杂度分析忽略了预处理/空间开销"
  - "下界与上界之间有未被讨论的 gap"
calibration_examples:
  - score: 0.3
    reason: "核心定理的证明有实质性漏洞"
  - score: 0.5
    reason: "证明大体正确但关键概率步骤跳步"
  - score: 0.85
    reason: "证明完整，仅 minor gap 可通过标准技术修补"
