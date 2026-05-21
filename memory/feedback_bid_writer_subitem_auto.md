---
name: bid_writer 按字数自动转子项级 + 模型单次吐字实测
description: writer 整章单次调用填不满大章;已改为章节目标字数>模型output_char_cap时自动转子项级;deepseek单次实测约8200字
type: feedback
---
**2026-05-20 实测 + 优化。**

**模型单次吐字能力实测**(prompt 要求"不少于12000字",测试时 max_tokens 临时override=16000):
- deepseek(V4-Flash):**8246 字** / 13304 总字符,113s — 快且稳,写作主力
- deepseek_chat:max_tokens=16000 下 8398 字 / 102s;config 默认 max_tokens=8000 时约 5000-5500 字
- sf_qwen_max(硅基流动 Qwen3.5-397B):**9007 字** / 176s — 产出最高,Qwen 家族,可做差异化第二模型
- qwen35(阿里云 dashscope):7020 字,但耗时 **1120s(18分钟)** — 能写但太慢,生产不实用
- sf_deepseek31:仅 3634 字 / 502s — 被 config max_tokens=8000 截断
- glm / kimi:长文本单次调用**3次重试全超时**(每次545s)——不可用于整章长文生成

**问题:** bid_writer 默认"单作者整章模式"——每章一次 LLM 调用。但 deepseek 单次只吐 ~8200 字,3 万字大章只能写到 40-60%(字数严重不足)。

**修复(已落地):**
1. `configs/bid_llm_config.json`:deepseek `output_char_cap` 12000 → **8000**(校准为实测值)。
2. `scripts/bid_writer.py`:`run_task` 增加自动判定——`章节目标字数 > 该章主模型的 output_char_cap` 且有子项 → 即时构建子项级计划(`_build_auto_subitem_plan`,全部子项用变体主模型)走 `_generate_one_cross`;否则整章一次。新增 `_single_call_safe_chars()` / `_build_auto_subitem_plan()`,`generate_one` 加 `chapter_plan_override` 参数。
3. 触发判据 = **内容字数**:`target_chars > output_char_cap`。子项级把大章拆成 N 个 ~5000 字子项,每个一次 deepseek 调用轻松写满。

**效果:** 养殖类项目三变体,子项级写出 V01 157K / V02 161K / V03 152K 字(达标 91-100%),采购大章 26-35K(单作者模式下只有 14-23K)。

**How to apply:** 多变体批量写作不再需要手动 `--cross-model` 或注入 chapter_model_plan;writer 按章节字数自动判定。新增/调整模型时,`output_char_cap` 必须填**实测单次吐字量**,不要填 max_tokens 理论值。关联 [[feedback_bid_plan_variants_redesign]]、[[feedback_bid_writer_scheduling]]。
