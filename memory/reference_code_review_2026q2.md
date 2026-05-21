---
name: bid-gen-v3 代码审查结论与改造路线图
description: 2026-05-21 对 bid-gen-v3(32脚本/23747行)做5轮多角度代码审查的汇总结论 + P0-P3 分期改造路线
type: reference
---
**2026-05-21 五轮代码审查(错误处理/数据契约/速度/编排/质量)汇总。**

## 五轮发现
- **R1 错误处理:** 202 处 except、2 处裸 except(bid_common.py:379,405)、34 处 except…pass、94 处 warn 级"已剥离/回退/跳过/未定义"。不区分"良性跳过"与"产出了但错的"。
- **R2 数据契约:** ~20 个中间文件在 32 脚本间传递,零 schema 校验;LLM 产出 JSON 无规范 schema,适配器靠 try/except 兜;rules 双轨共享块无同步。
- **R3 速度:** PDF OCR 逐页串行(290页≈25min,可并行到~4min,最大提速点);无增量提取,重跑全量重 OCR。
- **R4 编排:** bid_run.py 有 16 步/--resume/checkpoint/质量门禁,但缺:.XZZ 解包、Step-2 合成链不在 STEPS、不生成 format_sets、**组装后无产物验证门**、无 rules 同步。
- **R5 质量:** 32 脚本 24 个无测试(含最大最易错的 charts/assemble/writer);god 函数 render_site_layout 304行/parse_format_rules 305/postprocess_docx 219。

## 总病根
**流水线某步半失败时仍"成功"产出,且无自动化验证产出是否正确。** warn 不 fail + 无 schema 校验 + 无验证门 + 无测试,四层防线全缺 → bug 滑到交付前由人肉发现。

## 分期路线与进度(2026-05-21 当天全部推进)
- **P0 验证门 ✅已落地:** 新增 bid_verify.py(组装后硬核对封面/三家排版差异/图表md5/页数/危险日志),bid_run 加 verify 步;format_set 未定义改 ERROR;Word 页数检查超时修复。
- **P1 端到端缺口 ✅已落地:** .XZZ 自动解包;bid_rules_synthesize 生成 format_sets A-F;canonical_rules_path 把格式/暗标读 disqualification_rules.md 单一真源。(Step-2 合成链经核实 bid_run 已内联,无需补。)
- **P2 速度 ✅已落地:** vision OCR 由 2 路并行提到每模型 N 路(per_model_concurrency);bid_extract 按文件 md5 增量提取(--force 全量)。硅基流动视觉模型试过、因太慢已撤。
- **P3 质量地基 ✅已落地:**
  - P3.1 charts/assemble/writer 补特征化测试(原本零测试);P3.2 新增 validate_chart_data 图表数据校验。
  - P3.3 拆了两个最大 god 函数:render_site_layout 304行→55行编排+10个 _sl_draw_* helper(PNG md5 逐字节验证不变)、parse_format_rules 抽出 _normalize_rule_param_keys(输出 dict 逐字段验证不变)。
  - **P3.3 余留 ⬜:** postprocess_docx 219 / _fit_images 192 / _postproc_normalize_tables 177 / bid_writer.main 215 仍是 god 函数;改 docx 对象、特征化需真实 docx 夹具,留作真正改到时顺手拆。

测试 70→128 passed。关联 [[project_mozhugongka_chicken_farm_retro]]、[[feedback_verify_assembled_artifact]]。
