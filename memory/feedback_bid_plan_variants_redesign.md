---
name: bid_plan_variants 重构为"H1锁定+二级标题LLM差异化"
description: bid_plan_variants.py 核心机制改为一级标题逐字锁评分表原文、二级标题由LLM按不同维度差异化;废弃同义池改写+合并拆分
type: feedback
---
**2026-05-20 重构 `scripts/bid_plan_variants.py` 的核心机制。**

**旧机制(已废弃)的问题:** 原脚本用 `_TITLE_SYNONYMS` 同义池 + 子串匹配改写一级章标题,并按 Tier A/B 做章节合并/拆分。在**评分制 + 评分表是精确措辞**的项目上有致命盲区:章标题含"环境保护/施工方案/进度计划"等词会误命中通用同义池 → H1 被改写漂移。实测养殖类项目项目,ch07「保证措施(文明施工/安全/环保/人材机/冬雨季)」被改成「环保管理体系与措施」,把五合一综合章窄化成环保单章 → 评委按评分表对不上 = 丢分。

**新机制(当前):**
- **一级标题**:`build_variant_chapters` 逐字克隆基准 chapter_plan.md 的章标题,绝不改写/加前缀/换同义词/合并/拆分/重排。三家章标题完全一致 = 评分表原文。
- **二级标题**:`decompose_variant` 读 `evaluation_method.json` 的评分标准原文,按 `_DECOMP_STRATEGIES` 给每个变体分配一个**不同的切分维度**(流程时序 / 管理职能 / 对象专业 / 要点对应 / 目标递进 / 问题导向),LLM 据此为每章重新拟二级子项;子项数量可不同,但每套都必须完整覆盖评分标准。
- 章节数量/顺序与基准一致;只对字数预算做变体级缩放 + jitter。

**反串标分工(明确):** bid_plan_variants 管二级标题差异;编号体系差异归 bid_charts 编号风格轮转(B/C/L,见 [[feedback_heading_number_styles]]);正文差异归写作模型/温度/数据指纹;封面/排版/元数据归 bid_assemble。

**Why:** 评分制下一级标题必须逐字贴评分表(改字面=丢分,见 [[feedback_writer_h1_drift]]);反串标的标题差异化应落在二级子项的"组织逻辑"上,而非改一级标题文字。

**How to apply:** 多变体项目在 bid_variants 之后、bid_writer 之前跑 `bid_plan_variants.py`;它产出的 `V{XX}/chapter_plan_variant.md` 一级标题三家一致、二级子项三家按不同维度切分,writer 自动读取。不要再期望它改章标题或改章节数。
