---
name: 混合制项目按合格制写施组
description: 商务标评分+施组合格制不计分时,evaluation_method 强制归 passfail;brain.md 第五节和 chapter_plan 按合格制写,章节锚点 1:1 对齐必含大纲
type: feedback
session: 已脱敏
---
**规则**:项目同时存在「商务标分值」和「施工组织设计合格制不计分」时,evaluation_method 强制归为 `passfail`(不论商务标加起来多少分)。brain.md 第五节和 chapter_plan.md 都按合格制策略写——章节锚点必须 1:1 对齐 mandatory_outline.json 的 topics,**不能因为商务标有分就允许章节合并/吸收/省略**。

**Why**:仓储类项目B项目(2026-04-30)的 R5 根因复现——LLM 看到商务标 100 分,把整个项目判为评分制,Claude 在 brain.md 沿用评分制思维,把第七章第 16 项「其他」判为可选省略,16 项主题缩成 15 章 chapter_plan,后期返工才补回来。事实上对施组评审而言,**只有"施组本身是否计分"决定章节锚点策略**,商务标怎么评跟章节结构无关。

**How to apply**:
- bid_skeleton_extract.py 的 validate_b1 已强制规范化:`tech_score_is_passfail=true` → `evaluation_method="passfail"`,会留警告但不报错
- Claude 在 Step 2 看到 `evaluation_method=passfail` 但 `scoring_items` 仍列出商务标多个评分项(如报价 50 分 + 信用 25 分 + 业绩 5 分 + 项目经理 12 分 + 班子 8 分),识别为混合制,brain 第五节如实抄录商务评分表 + 显著标注"施工组织设计采用合格制不计分"
- chapter_plan 章节数 = mandatory_outline.json topics 数量,严禁基于商务分值做合并
- bid_plan_verify.py 第 8 条铁律会校验 brain 第七节 topics 数 = JSON topics 数,缺失硬失败
