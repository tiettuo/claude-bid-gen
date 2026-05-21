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

## 分期路线(按 ROI)
- **P0 验证门:** bid_run 加 `verify` 步,组装后硬核对(封面/三家字体行距页边距是否真不同/图表md5/页数≤顶/WARNING);format_set 未定义改 ERROR。
- **P1 端到端缺口:** .XZZ 自动解包;format_sets 自动生成;rules 双轨改单一真源;Step-2 合成链入 STEPS。
- **P2 速度:** PDF 页级 OCR 并行;增量提取(hash 跳过)。
- **P3 质量地基:** charts/assemble/writer 补测试;LLM-JSON 规范 schema+校验;拆 god 函数。

详细实施计划见对话记录(2026-05-21,P0+P1 逐文件改动点)。关联 [[project_mozhugongka_chicken_farm_retro]]、[[feedback_verify_assembled_artifact]]。
