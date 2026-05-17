---
name: bid-gen Step1 研究成果
description: bid-gen 招标文件提取 Step 1 自主研究成果(2026-05-05),含 17 条决策清单 + 5-6 天 MVP 改造方案
type: reference
session: 已脱敏
---
研究目录:`~/research/bid-gen-extraction/`
- `SUMMARY.md` — 一页纸总结(决策清单 + MVP 工时 + ROI Top3)
- `notes.md` — 完整 5 轮笔记(方法论/代码实证/外部对标/MVP 设计/收尾)
- `progress.md` — 5 轮研究进度记录

## 何时使用
- 用户问"bid-gen Step 1 怎么改 / 应该怎么提取 / 现状什么问题"
- 准备实施 P0 改造(templates/tibet_gov_simplified.json 等)
- 评估 OCR 引擎选型(GLM-OCR vs Qwen-VL)

## 核心结论(3 句话)
1. 方法论文档(bidder_framework/Step1_提取方法论.md)已设计 5-Tier MIS,但代码层 P0 零进展
2. ROI 最高三项:范本零提取(4h)/ Tier1 正则优先(1d)/ Tier4 强制视觉抽 10.17(1d)
3. 2026 OCR 格局变了:GLM-OCR 0.9B 在中文表格场景已超 Qwen-VL,fallback_order 应分场景

## 整合方案
新建 `bid_extract2.py` 与旧管道并存,通过 `bid_run.py --new-pipeline` 切换,零破坏可回滚。
