---
name: bid-gen
display_name: 投标技术标生成
display_name_en: Tender Technical Proposal Generator
description: 投标技术标生成（bid-gen-v3）：Use when the user mentions 招标/投标/投标文件/施工组织设计/技术标/施组/废标/暗标/评分表/控制价/工程量清单/招标文件.pdf/图纸, or points at a tender/RFP directory or a tender .zip and wants to generate, review, assemble, or debug a Chinese construction-tender technical proposal (施工组织设计). Also use when the user asks to run bid-gen, fix bid-gen scripts, apply bid-gen memory feedback, or continue/resume an in-progress bid run. Trigger even when the user does not say 'bid-gen' explicitly — any 工程招投标 / 施工组织设计 generation intent maps here.
description_zh: 招标文件 PDF/压缩包进，可直接提交的施工组织设计 DOCX 出。按招标第七章大纲逐项成章、按页数硬顶倒推字数、跑完废标合规与格式门禁，支持暗标与多标段防围标。
description_en: From a Chinese tender package (PDF/ZIP) to a submission-ready 施工组织设计 DOCX — outline-anchored chapters, page-cap-driven budgeting, compliance and format gates, blind-bid and multi-bidder anti-collusion support.
examples_zh:
  - "把这个招标 zip 里的项目做一份施工组织设计，页数别超招标限定的 70 页"
  - "招标文件是合格制，技术标不计分，帮我按第七章编制大纲逐项成章"
  - "检查我这份技术标会不会被废标：页数、字体字号、暗标有没有泄漏公司名"
  - "两家公司投同一个标段，帮我出两版差异化但都对大纲的技术标"
examples_en:
  - "Generate a construction organization design DOCX from this tender zip, staying under the 70-page cap"
  - "The technical proposal is pass/fail here — follow the chapter outline in chapter 7 one-to-one"
  - "Check whether this proposal would be disqualified: page count, fonts, blind-bid identity leaks"
  - "Produce two differentiated technical proposals for two bidders on the same lot"
version: 3.2.0
author: "tiettuo"
license: MIT
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Bid Gen

Run the **bid-gen-v3** pipeline — a script-backed generator for Chinese construction-tender
technical proposals (施工组织设计). You orchestrate Python scripts; you do **not** improvise
prose from memory. Everything user-visible is produced by `scripts/*.py`.

技能根目录 = 本 SKILL.md 所在目录。下文 `<SKILL_DIR>` 均指它，脚本一律从该目录调用：

```bash
cd "<SKILL_DIR>" && python3 scripts/bid_run.py --doctor
```

## 第 0 步：先体检，再动手（不要跳过）

```bash
cd "<SKILL_DIR>" && python3 scripts/bid_run.py --doctor
```

它一次查清：Python 包（python-docx / PyMuPDF / pdfplumber…）、51 个脚本是否齐全、
LLM 配置与 API Key、外部二进制（**pandoc 是 Step 4 组装 DOCX 的硬依赖**）、中文字体。

- `✓ 全部检查通过` → 继续
- 缺包 → `pip install -r requirements.txt`
- 缺 pandoc → macOS `brew install pandoc` / Windows `winget install JohnMacFarlane.Pandoc`
- 缺 API Key → 在 `~/.env` 写 `GLM_API_KEY=` / `DEEPSEEK_API_KEY=` / `DASHSCOPE_API_KEY=` 等
  （或用 `LLM_ENV_FILE=` 指定别的文件），配置模板见 `scripts/configs/bid_llm_config.json`

**平台差异**：目录页页码回填（`bid_toc_fill`）依赖 Microsoft Word 导出 PDF。
- macOS + Word → 全自动
- 其他环境 → 该步降级（页码落成「—」），其余流程不受影响；提示用户装 Word 或接受目录无页码

## 输入：招标 zip（推荐入口）

用户常常只给一个 `.zip`。**先入包，再跑管道**：

```bash
cd "<SKILL_DIR>" && python3 scripts/bid_ingest_zip.py "<项目>.zip" --out-dir "<父目录>"
```

它会递归解开 `.XZZ/.GKZJ/.ZBWJ/.ZBF/.ZBJ` 这些"假 zip 真容器"，把
`招标文件.pdf` / `控制价清单文件.pdf` / `招标公告.pdf` / `工程量清单/` / `图纸/`
归一化到项目根——整条管道只认这个布局。`--name` 可指定项目文件夹名（它会流向最终 DOCX 文件名）。

## 主流程

```bash
cd "<SKILL_DIR>"
python3 scripts/bid_run.py "<tender_dir>"                    # 全流程
python3 scripts/bid_run.py "<tender_dir>" --to chart_specs   # 只跑到规划（推荐先看规划再写）
python3 scripts/bid_run.py "<tender_dir>" --from write       # 从写作继续
python3 scripts/bid_run.py "<tender_dir>" --resume           # 断点续跑
python3 scripts/bid_status.py "<tender_dir>"                 # 只读进度
```

单份与批量（≥2 家投标人）由 `extracted/variants.json` 是否存在自动判定。批量模式会做
**跨投标人相似度检查**（防串标认定）+ 每家格式差异化。

### 三条必须先读招标原文才能定的参数

跑 `write` 前必须已经确认下面三件事，它们全在 `extracted/` 的产物里，**不要凭印象**：

1. **锚点**：`mandatory_outline.json`（第七章编制大纲）∪ `evaluation_method.json`（评分表）。
   锚点标题**逐字照抄**，不改写不合并。
2. **页数硬顶**：`disqualification_rules.md` 的 `page_hard_cap`。招标常写
   「大型复杂 ≤100 页 / 中小型 ≤70 页」这种**例举式**二选——按 `bid_plan_synthesize`
   的 4 维矩阵（控制价 / 工期 / 工程类型 / 单专业体量）自动判档，**不要让用户二选**，
   判不准时取保守档（超页即废标，少写只损失篇幅）。
3. **评标方式**：`tech_evaluation_method` 是 `score` 还是 `passfail`。
   **合格制**（技术标 0 分）下大纲一级主题必须 1:1 独立成章——包括「其他」——
   缺一项即被视为不响应实质性要求。二级/三级节也按原文标题 1:1 保留，
   正文用 ★实质 / ○简化 / △不涉及 三档标注，不涉及的要写清理由，不许删标题。

### 页数是一次算对的，不是写崩了再删

`bid_plan_synthesize --target-pages N` 按「页数 = 封面 + 目录(条目/30) + 图表(张×0.85) + 文字页」
反推目标字数。**图表数必须先于写作定死**：`bid_chart_specs` 会按章派生一堆图，
若 plan 里按 5 张算、实际渲染 12 张，页数当场超 2-3 页。

改 `chapter_plan.md` 的图表列后，必须同步裁剪 `extracted/chart_specs.json`，
否则 `bid_compliance` 的「图表覆盖」会报 advisory 未落地。

## 门禁（判产物，不看退出码）

```bash
python3 scripts/bid_verify.py     "<tender_dir>"              # 封面/字体/页边距/页数硬顶
python3 scripts/bid_compliance.py "<tender_dir>" --all-variants  # 废标条款/大纲覆盖/格式实测
```

- `verify` 用 **Word 实测页数**（macOS 权威口径，LibreOffice 不准）比对硬顶
- `compliance` 查暗标泄漏、编造标准、工期越界、大纲主题缺失、图表未落地
- 详细人读报告在 `output/verify_report.md`、`output/compliance_summary.md`、
  `output/V*/reports/compliance_scan.md`

**过门禁 ≠ 交付**。开 DOCX 亲自看：封面、目录页码是否连续、正文首行缩进与行距、
以及第 7 章这类大章的二级/三级标题是否真在目录里。

## 写作阶段的三条硬规矩

1. **数字必须来自清单**（`extracted/boq.json`）。写到工程量就引用真实值
   （如 `挖基坑土方 1669.01m³`、`HRB400Φ25 3.223t`），不写"约""若干""大量"。
2. **跨章一致**：同一设备/人员/工序在「施工方案」章与「附表」章的数量规格必须一致。
   分章并行生成时最容易各自编，写完全局搜一遍关键数字。
3. **不编造企业资产**：设备表、检测仪器表、劳动力表的数字若没有企业档案来源，
   用 `[待填]` 占位并明确告诉用户「这三张表要按贵司实际填」，不要编型号数量。

## 复用记忆（改脚本前必读）

`references/memory/` 是真实投标翻车记录沉淀，每条都对应一条校验规则：
- `feedback_bid_pass_fail_anchor.md` — 合格制漏掉「其他」章导致不响应
- `feedback_bid_size_judge_matrix.md` — 70/100 页判档矩阵
- `feedback_bid_b3_implicit_constraint.md` — 隐性硬约束（不得超过/至少/视为）抓取
- `feedback_verify_assembled_artifact.md` — 只验退出码不验产物
- `feedback_bid_no_hardcode_size.md` — 格式不许硬编码

完整操作合同见 `references/commands/bid-gen.md`（Step −1 到 6）；
抽取方法论见 `references/extraction-methodology.md`；图表 schema 见 `references/docs/chart_schemas.md`。

## 交付话术

跑完把这几件事讲清楚，别只说"完成了"：
1. **实际页数 vs 硬顶**（这是最容易废标的点）
2. **大纲覆盖**：N 个主题 / 命中 N 个
3. **还需要用户补的**：企业设备人员业绩、投标单位名称、盖章签字
4. 中间产物在 `output/`，改哪章改对应 `.md` 后 `--from assemble` 重出稿
