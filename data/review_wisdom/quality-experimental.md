# 评审智慧库：quality 赛道实验论文

venue: ICML / KDD / WWW
paper_type: experimental
rubric_patterns:
  - "图表标题必须自描述（不依赖正文才能理解）"
  - "表格中的最佳结果必须加粗标注"
  - "实验部分的结构应按研究问题组织（非按数据集罗列）"
common_critiques:
  - "实验结果用文字描述但没有对应的图表"
  - "表格格式混乱（精度不一致/单位缺失）"
  - "图表过多但信息密度低"
calibration_examples:
  - score: 0.5
    reason: "实验充分但呈现混乱"
  - score: 0.7
    reason: "呈现清晰但图表需要加标签"
  - score: 0.85
    reason: "结构清晰+图表自足+表格规范"
