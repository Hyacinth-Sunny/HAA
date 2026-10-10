# 评审智慧库：fidelity 赛道

venue: 通用
paper_type: systems
rubric_patterns:
  - "简报铁律块每条约束必须逐项核对（缺失即重大偏离）"
  - "排除方向（明确不做）出现在论文中 = 直接 reject"
  - "知识文件清单中的组件必须在论文中透明处理"
common_critiques:
  - "论文整体质量高但完全偏离了简报委托的主题"
  - "简报要求的某个核心实验/分析在论文中缺失"
  - "使用了简报明确排除的技术路线"
calibration_examples:
  - score: 0.3
    reason: "架构整体偷换，简报核心问题消失"
  - score: 0.7
    reason: "主体忠实但缺少简报要求的某个子实验"
  - score: 0.95
    reason: "全部约束逐项兑现，排除方向零违反"
