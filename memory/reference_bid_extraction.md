---
name: bid-gen Step 1 完整管道 (v2 + 5-Tier MIS)
description: 西藏招标项目自动化提取完整管道,extract→template→facts→derive→skeleton→special→input_assemble,产 7 份 JSON+brain.md,一行命令端到端
type: reference
---
**完整方法论文档**:`skills/bid-gen/references/extraction-methodology.md`
**研究背景**:`~/research/bid-gen-extraction/{SUMMARY,notes,progress}.md`

## 一行命令(新项目入口)

```bash
python scripts/bid_run.py "/path/to/new_tender/"
```

自动顺序:`extract → template → facts → derive → skeleton → special → input_assemble → fingerprint → chart_specs → write → review → assemble → compliance → similarity → summary`

## 8 份机器产物 (extracted/)

| 文件 | Tier | 工具 | 说明 |
|------|------|------|------|
| template_match.json | 0 | bid_template_match.py | 西藏范本指纹比对(西藏/简易综合评估法/公共资源交易等关键词≥2 命中即加载常量) |
| project_facts.json | 1 | bid_facts_extract.py | 13 字段(项目名/编号/地点/招标人/代理/资金/控制价/工期/开竣工/质量/资质/范围),正则优先 LLM 兜底,每字段带 source page+method |
| project_derived.json | 2 | bid_derive.py | 海拔(74 县库,县级优先)+altitude_class(low/middle/high/very_high/extreme)+season_set+spans_winter+size_class |
| evaluation_method.json | (v2 骨架) | bid_skeleton_extract.py B1 | 评标方法 score/passfail+评分项原文+否决项,passfail+商务有分自动归 passfail |
| mandatory_outline.json | (v2 骨架) | bid_skeleton_extract.py B2 | 第七章必含主题(found+topics+attachments) |
| disqualification.json | (v2 骨架) | bid_skeleton_extract.py B3 | 全部废标条款(category 分类+source_page+source_method),三项目实测 58-71 条 |
| special_clauses.json | 4 | bid_special_extract.py | 邮政业务/廉政/人工费 20%/农民工专户/区外备案 等项目特殊条款,LLM 抽 |
| project_input.json | 合并 | bid_input_assemble.py | Tier 0+1+2+4+skeleton 合并+rejection_lines(70-80 条扁平避雷) |

## brain.md 自动翻译

```bash
python scripts/bid_brain_translate.py "/path/to/tender/" --force
```

第 1-4 节:facts/derived/special 数据组织+保留旧 brain 第 3 节人工分析
第 5/6/7 节:1:1 翻译 evaluation/disqualification/mandatory_outline JSON

## 8 条铁律 (bid_plan_verify.py)

1 评分总分一致 / 2 三圈字数 / 3 子项覆盖 / 4 总页数硬顶 / 5 大纲覆盖 /
6 来源页码集中度(R4 防御) / 7 骨架 JSON 存在性 / 8 brain↔skeleton 对账(R1 防御)

合格制项目 + chapter_plan 仍带商务标分值 → 容忍(v1/v2 切换警告,不阻断)

## 历史回归测试基准

- 仓储类项目A ✓:范本命中,facts 13/13,海拔 3766m/very_high,小型,8 条特殊
- 仓储类项目B   ✓:范本命中,facts 13/13,海拔 4500m/extreme,小型,10 条特殊
- 学校类项目 ✓:范本命中,facts 13/13,海拔 3766m/very_high,小中型,6 条特殊

三项目验证:tier0 范本逐字相同 / tier1 13 字段全命中 / tier4 ≥ 3 条特殊条款 / 邮政业务+廉政+人工费抽出 / 仓储类项目B=extreme

## 西藏范本固定常量

`templates/tibet_gov_simplified.json`:16 章主题 + 5 附表 + B1.x 31 条 + 1.4.3 24 项 + 10.16 格式 + 6 项评分公式骨架

## 何时使用本指针

- 用户说"新项目开始" / "跑 bid-gen" / "提取这个招标"→ 跑命令一行
- 用户问"v2 怎么跑 / 7 份 JSON 是什么"→ 看本指针 + 方法论文档
- 出问题查 ~/research/bid-gen-extraction/notes.md(原研究 5 轮笔记)

## 旧版本

v1 方法论(6 prompt + 7 类 schema)在 `招标文件信息提取方法论.md` 已丢失,本管道完全替代。
