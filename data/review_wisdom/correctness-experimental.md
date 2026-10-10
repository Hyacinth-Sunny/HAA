# 评审智慧库：correctness 赛道实验

venue: ICML / KDD / WWW
paper_type: experimental
rubric_patterns:
  - "实验设置必须完整描述（数据/模型/超参/随机种子）"
  - "对照组必须公平（相同调参预算）"
  - "结论必须由数据支撑（不能数据弱结论强）"
common_critiques:
  - "缺少 error bar 或置信区间"
  - "消融实验不完整（关键组件未单独验证）"
  - "结论泛化过度（单一数据集/单一模型架构）"
calibration_examples:
  - score: 0.4
    reason: "实验跑通但缺少对照组和消融"
  - score: 0.6
    reason: "有对照但无统计显著性"
  - score: 0.8
    reason: "设置完整+有 error bar+消融齐全"
