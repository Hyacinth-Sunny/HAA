# 评审智慧库：industry 赛道基准测试

venue: MLPerf / TPC / SPEC
paper_type: benchmark
rubric_patterns:
  - "基准必须覆盖典型负载（不能只测 cherry-picked 场景）"
  - "可重复性：代码和数据必须可获取或有明确的获取路径"
  - "评分指标必须与声称的优化目标一致"
common_critiques:
  - "基准选择有偏差（只测了方案占优的场景）"
  - "缺少统计显著性检验（单次运行的差异可能是噪声）"
  - "指标定义与论文声称的优化目标不对应"
calibration_examples:
  - score: 0.4
    reason: "基准覆盖面窄，结论不具推广性"
  - score: 0.7
    reason: "覆盖面够但缺统计检验"
  - score: 0.85
    reason: "覆盖全面+可重复+有显著性检验"
