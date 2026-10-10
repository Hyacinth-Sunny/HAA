# 评审智慧库：correctness 赛道冷启动

venue: ICDE / SIGMOD / VLDB
paper_type: systems
rubric_patterns:
  - "正确性论证必须覆盖所有声称的不变量——缺失一条即 major"
  - "协议描述必须明确消息丢失/重复/乱序场景下的行为"
  - "性能声明必须给出实验配置（硬件/数据规模/并发度）"
common_critiques:
  - "论文声称 X 但实验只验证了 X 的子集"
  - "正确性证明依赖未声明的假设（如故障模型不完整）"
  - "与 baseline 的比较不公平（不同硬件或不同数据）"
calibration_examples:
  - score: 0.3
    reason: "核心机制无证明，只有直觉描述"
  - score: 0.6
    reason: "证明骨架在但关键引理跳步"
  - score: 0.8
    reason: "证明完整但实验仅验证了单点配置"
