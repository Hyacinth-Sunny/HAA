# 评审智慧库：quality 赛道理论论文

venue: STOC / FOCS / SODA
paper_type: theory
rubric_patterns:
  - "定理陈述必须自包含（所有符号在附近定义）"
  - "证明结构必须有引理分层（不是一大块）"
  - "边界情况（空集/零输入/极端参数）必须显式讨论"
common_critiques:
  - "定理条件过强，削弱了实际适用性"
  - "证明中某步'显然'实际需要非平凡论证"
  - "算法描述与正确性证明之间有缝隙"
calibration_examples:
  - score: 0.5
    reason: "证明正确但陈述含混（读者需猜条件）"
  - score: 0.7
    reason: "陈述清晰但引理跳步"
  - score: 0.9
    reason: "完整自包含，边界情况讨论到位"
