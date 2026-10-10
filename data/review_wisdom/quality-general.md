# 评审智慧库：quality 赛道冷启动

venue: NeurIPS / ICML / ICLR
paper_type: general
rubric_patterns:
  - "行文清晰度：术语首次出现必须定义，核心概念不得多处给出不同定义"
  - "结构完整性：intro 必须在 3 段内说清问题/方案/贡献"
  - "图表自足：每张图/表必须独立可读（标题+轴标签+图例齐全）"
common_critiques:
  - "摘要承诺了正文没有兑现的内容"
  - "相关工作节遗漏了最近 2 年的关键工作"
  - "数学记号不一致（同一符号在不同节含义不同）"
calibration_examples:
  - score: 0.4
    reason: "结构混乱，读者需要自行拼凑逻辑线"
  - score: 0.6
    reason: "结构清晰但术语未定义，图表缺轴标签"
  - score: 0.8
    reason: "行文流畅，仅少数 typos 和格式不一致"
