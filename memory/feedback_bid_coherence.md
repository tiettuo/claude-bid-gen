---
name: 施组连贯性是架构问题——靠主线注入+生成后勾兑,不是靠单章写好
description: 章节散装的根因是"每章独立并行生成+难点全量dump+无跨章引用";解法是三道工序(主线注入/引言重写/跨章勾兑),保持并行速度的同时补上叙事连贯性
type: feedback
---

施组读起来"散装"(像 N 篇独立文章拼接)时,**根因不在单章质量,而在架构**:每章在 `ThreadPoolExecutor(workers=4)` 里完全独立生成,章 N 看不到章 N-1 任何一个字;difficulties.json 的 5 条难点被一模一样地复制粘贴进每章 prompt(`_build_block1_project` 的 `[:5]`),无"本章负责哪个难点"的映射;正文零「详见第X章」引用;跨章矛盾检查只查数值且恒不阻断。

**Why:** 评分合规(每章 1:1 评分表)和叙事连贯(章节互相呼应)是两个独立的设计目标。原管道只优化前者,结构性牺牲后者。领域方法论(民航局规范/项目总工纲要)明确:施组应以「总体施工部署」为纲领、以进度为主线串联各环节,各章"逻辑清晰、前后呼应"。没有部署纲领这个锚点,每章只能各写各的。

**How to apply(三道工序,均受 `configs/bid_coherence.json` 开关控制,默认 off 灰度):**

1. **工序A 主线注入(plan 阶段)**:`bid_deployment_synthesize.py` 单次 LLM 产出「总体施工部署纲领」(`deployment_outline.json`:部署声明+5维部署+难点→维度映射) → `bid_plan_synthesize` 派生 `chapter_narrative.json`(每章叙事维度+聚焦难点) → `bid_writer._build_block1_project` 从全量 dump 改为精准注入(只列本章聚焦的 2-3 条难点 + 主线回指声明)。**关键:精准注入后 prompt 反而更短(控 context 稀释)。** 无 narrative 时走旧的全量 dump(降级安全)。

2. **工序B 章首引言重写(post-writer)**:`bid_chapter_intro.py` 在 write+h1_fix 后,每章独立调一次 LLM 只重写 H1 后的引言段——回指部署纲领+承接前章+点明叙事职责。**只动引言段,H1/##/正文不动。** 每章是独立短调用(输入~600字输出~300字),不让一个模型连续写到底。

3. **工序C 跨章勾兑(assemble 前)**:`bid_coherence_pass.py` 三次独立 LLM 调用:C-1 引用编织(各章摘要→「详见第X章」回写)、C-2 重复检测(调 `bid_concise` 局部精简冗余章)、C-3 矛盾修复(复用 `bid_expert_review.cross_chapter_check`)。落在 `STEPS.coherence_pass`(review 后 assemble 前)。

**关键约束(踩坑预防):**
- **C4 context 稀释**:模型写久了/上下文太长能力下降 → 绝不把前文全文注入,只用摘要(每章≤300-400字);每个新调用是独立短上下文。这是"用摘要而非全文"的核心理由。
- **C1 评分表锚点**:H1 标题仍 1:1 评分表;第0章「编制总说明」(`00_编制总说明.md`)不对应评分项,作编制说明页,不计入页数预算。
- **C2 防串标**:引言重写 per-variant 用 `assign_strategy` 不同叙事角度;第0章作编制说明可各变体相同。
- **C3 可回滚**:`bid_coherence.json enabled=false` → 管道行为与改动前完全一致;`chapter_narrative.json` 不存在 → writer 走旧逻辑;`coherence_pass` 失败不阻断 assemble(降级跳过)。

**架构哲学**:生成阶段保持并行独立(保速度+防 C4),连贯性靠"主线预注入"+"生成后缝合"两道独立工序补上——正是茅台的"勾兑":原酒各自酿造(并行),出厂前勾兑融合(后处理)。不要试图让 writer 改成顺序生成(会拖慢+context 累积)。
