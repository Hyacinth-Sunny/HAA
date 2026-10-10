# 评审智慧库：industry 赛道系统工程

venue: OSDI / SOSP / NSDI / ATC
paper_type: systems
rubric_patterns:
  - "工业相关性必须回答'谁在什么场景下会用'"
  - "部署成本（硬件/运维/迁移）必须量化"
  - "与现有生产系统的兼容性必须讨论"
common_critiques:
  - "方案在理想条件下有效但未考虑生产环境的故障恢复"
  - "声称的性能提升在生产负载下可能不复现"
  - "缺少与至少一个工业级 baseline 的对比"
calibration_examples:
  - score: 0.3
    reason: "纯学术玩具，无真实场景动机"
  - score: 0.6
    reason: "动机真实但方案在现有基础设施上不可部署"
  - score: 0.8
    reason: "解决真实痛点，部署路径清晰，但缺 TCO 分析"
