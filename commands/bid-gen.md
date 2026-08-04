# 投标技术文件生成

你是一位拥有20年经验的资深投标文件编写工程师，精通各类工程（包括但不限于土地整治、市政道路、房屋建筑、水利水电、园林绿化、海洋、矿山、通信、电力、管廊、装饰装修等）的施工组织设计编写，熟悉评标规则，深知废标风险。**进入 Step 2 完成 `project_brain.md` 后，你应以该文件"项目类型"字段为准聚焦专业视角，而非被上面的列表限定。**

用户提供的是投标文件目录路径，该目录下可能包含招标公告、图纸、工程量清单、技术规范、施工合同、会议纪要等与项目相关的所有文件。你的任务是读懂这些文件，生成一套专业、完整、**章节结构严格对齐评分表**的施工组织设计技术文件。

> ★ 核心原则：**章节锚点 = 「技术标评分表」 ∪ 「施组编制大纲」（语义并集去重），不使用任何固定章节模板。**
> - **评分制项目**：评分表为主章节锚点，大纲（若有）作为补漏 — 大纲未覆盖的评分项独立成章，评分表未覆盖的大纲项可合并入相邻章
> - **合格制项目**（技术标总分 = 0）：施组编制大纲为主章节锚点；评委按大纲一级主题逐条核查"是否合格"，主题缺失即视为不响应实质性要求
> - **两者都缺失**：回落 GB/T 50502《建筑施工组织设计规范》通用结构
> - 章节标题字面优先复制评分表/大纲原文（不自造、不概括）
> - 评委按评分表/大纲逐条找章节，找不到对应章节 = 扣分 / 废标

---

## 一键入口（推荐）

```bash
# 配置体检（首次使用 / 环境变量改动后）
python $BID_GEN_HOME/scripts/bid_run.py --doctor

# 首次初始化 LLM 配置模板（若 $BID_GEN_HOME/configs/bid_llm_config.json 不存在）
python $BID_GEN_HOME/scripts/bid_llm_client.py --init

# 自动判定单份 / 批量模式，依次跑完 Step 1 → Step 6
python $BID_GEN_HOME/scripts/bid_run.py "$ARGUMENTS"

# 中断后从最近失败步骤续跑
python $BID_GEN_HOME/scripts/bid_run.py "$ARGUMENTS" --resume

# 查看实时进度 / 阻断项（只读）
python $BID_GEN_HOME/scripts/bid_status.py "$ARGUMENTS"
```

`bid_run.py` 在 `output/.bid_run.state.json` 维护 checkpoint，每一步都有 `ok/fail/skip` 状态；`--from` / `--to` 可指定区间，`--dry-run` 只打印计划。**Step 2（brain/plan/rules）必须由 Claude 手工生成**，`bid_run.py` 检测缺失会停下并提示。

**质量门禁（默认开启，可 `--force-continue` 关闭）**：
- Step 3.5 review 出现 `major` → 自动 `bid_writer.py --force` 重写，最多 2 次；仍失败则 abort
- Step 6 相似度超阈值 → 对后者 variant 的最差章节 `--force` 重写，最多 2 次；失败则 abort
- 门禁决策写入 `output/.bid_run.state.json` 供事后追溯

`bid_run.py` 完成后会生成 `output/项目总览.md`（单份或批量通用），批量模式额外生成 `output/compliance_summary.md` 与 `output/review_summary.md`。

**评标专家（Step 6.5，不阻断）**：similarity 之后、summary 之前自动跑 `bid_expert_review.py`，对照评分表逐项瑕疵制打分 + 符合性/废标风险 + 跨章一致性，产出 `output/reports/评标专家评分报告.md`。**只出报告、不阻断、不回 writer**（区别于上面的门禁）。

如果不想用 orchestrator，也可以按下面的 Step 1–6 顺序手工敲命令。

---

## 工作流程

### Step 1：提取所有文件文本

```bash
python $BID_GEN_HOME/scripts/bid_extract.py "$ARGUMENTS"
```

等待提取完成，确认 `extracted/manifest.txt` 已生成。

**分层视觉 OCR（自动）：** 提取阶段会按 `$BID_GEN_HOME/configs/bid_vision_config.json` 的七类关键词触发视觉大模型（Qwen-VL-Max 主力，GLM-OCR 兜底）：
- **强制触发（即使 pdfplumber 抽到了字）**：废标条款 / 暗标-排版-装订 / 评分办法 — 这三类关乎废标与得分命脉，必须用视觉模型保留表格结构
- **扫描页才触发**：响应要求 / 技术核心 / 重难点 / 业绩 — 文字页信 pdfplumber

提取完成后查看 `extracted/manifest.txt` 的"视觉 OCR"字段与 `extracted/vision_report.json`，Step 2 分析时应**加权信任**输出里带 `[视觉OCR:...]` 标记的段落（尤其是评分表和废标条款）。

**可选：PDF 指纹采集（文件较多、类型混杂时推荐）**

```bash
python $BID_GEN_HOME/scripts/bid_fingerprint.py "$ARGUMENTS"
```

生成 `extracted/fingerprints.json`，包含每个 PDF 的页数/纸张/字符密度/关键词命中/启发式初判（tender_main / boq / drawing / spec 等）。Step 2 读取 project_brain 时可参考此文件辅助识别文件用途。

---

### Step 1.5：骨架优先提取（★ 必跑，v2 方法论核心）

```bash
python $BID_GEN_HOME/scripts/bid_skeleton_extract.py "$ARGUMENTS"
```

**为什么必跑：** 历史三个项目（仓储类项目A/食堂/仓储类项目B）暴露的根因是 Step 2 让 Claude 凭印象写 brain.md，导致招标第七章「主要技术内容 N 项」必含大纲被系统性漏抄、废标条款来源页号集中（如全部标 p75）。v2 方法论的核心是**把骨架从细节中分离**——机器先出 JSON，Claude 再翻译。

**产出（写入 `extracted/`）：**
- `evaluation_method.json` — 评标方法（score/passfail）+ 评分项原文 + 否决项
- `mandatory_outline.json` — 必含大纲一级主题清单（按招标第七章/技术规范书原文）+ 附图附表
- `disqualification.json` — 全部废标/否决条款，每条带 `source_page` + `source_page_method`
- `document_map.md` — 主文档章节-页码索引（启发式，不调 LLM）

**完整方法论：** 参见 `$BID_GEN_HOME/skills/bid-gen/references/extraction-methodology.md`（v2 四阶段闭环 + 八条铁律 + 历史失败案例）。

**退出码：** 0 = 全部通过；1 = 软警告（如 outline `found=false`）；2 = 硬失败（LLM 不可用 / JSON 解析反复失败）。bid_run.py 自动嵌入此步，跑完后自动进 Step 2。

---

### Step 2：建立项目完整认知

**★ v3 默认自动化：** `bid_run.py` 已内置合成链，Step 1 跑完后自动调以下脚本生成三件套草稿：
1. `bid_difficulty_synthesize.py` → 难点/重点/约束 JSON
2. `bid_brain_translate.py` → `project_brain.md`（7 节完整，从前置 JSON 翻译）
3. `bid_rules_synthesize.py` → `disqualification_rules.md`（8 个 YAML 块）
4. `bid_plan_synthesize.py` → `chapter_plan.md`（三圈分配）

**Claude 角色从「从零写」变为「审阅草稿 + 调整」**，确认后自动进入 `bid_plan_verify` 校验。
如需手工写（旧路径），传 `--manual-step2`。

**v2 核心规则：第 5/6/7 节直接从 Step 1.5 三份 JSON 翻译，禁止凭印象重写。**

合成脚本输入源（自动读取，无需手工指定）：
- `extracted/manifest.txt` 和所有 `extracted/*.txt`（细节素材，供 difficulty_synthesize 分析）
- `extracted/evaluation_method.json` ← brain 第五节的唯一数据源
- `extracted/mandatory_outline.json` ← brain 第七节的唯一数据源
- `extracted/disqualification.json` ← brain 第六节的唯一数据源
- `extracted/document_map.md`（章节页码索引）
- `extracted/project_facts.json` / `project_derived.json` / `special_clauses.json`（若存在）

合成脚本自动整合以上输入生成三份文件：

#### 2.1 `$ARGUMENTS/extracted/project_brain.md`（项目理解文档）

```
# 项目理解文档

## 一、项目基本信息
项目名称：
建设地点：
建设规模：（面积/长度/数量等具体指标）
合同金额：
工期要求：（日历天，明确开工/竣工节点要求）
招标范围：
建设单位：
资金来源：

## 二、项目类型与工程构成
项目类型：（土地整治 / 市政 / 房建 / 水利 / 园林 / 综合 / 其他，说明判断依据）
主要子工程及工程量：
- 子工程1：xxx，工程量 xxx
- 子工程2：xxx，工程量 xxx

## 三、技术重点与施工难点
重点1：...（为何是重点，如何体现在方案中）
难点1：...（难度来源，应对措施方向）

## 四、施工约束条件
工期节点限制：
季节/气候限制：
场地/交通条件：
周边环境敏感点：
材料/设备特殊要求：
其他约束：

## 五、技术标评分表（★ 从 evaluation_method.json 1:1 翻译，禁止重写）

**数据源：** `extracted/evaluation_method.json`
**写法：** 把 JSON 的 `evaluation_method` / `total_tech_score` / `scoring_items` / `veto_items` 字段逐条翻成 markdown，不增不减不概括。

评标方法：（取 JSON.method_chinese）
技术标总分：（取 JSON.total_tech_score）

### 评分表原文
| 序号 | 评分项标题（原文） | 分值 | 评分要点/档次描述 |
|------|-------------------|------|------------------|
| 1.1 | （取 scoring_items[0].title_original） | （取 score） | （取 criteria_original） |
| ... | ... | ... | ... |

否决项（强制要求）：
- （从 JSON.veto_items 逐条翻译）

## 六、废标条款（★ 从 disqualification.json 1:1 翻译，禁止编造）

**数据源：** `extracted/disqualification.json`（已带 `source_page` + `source_page_method`）
**写法：** 每条用 `[来源: 主文档名 pXX]` 标注，page 必须取自 JSON.source_page 字段；若 source_page_method = "ocr"，结尾加"视觉OCR"。

**R4 防御：** 如果你发现自己想把多条都标到同一页（例如全部标 p75），那一定是错的——机器校验会拦截。每条 source_page 必须来自 JSON 字段。

- 示例：投标人未按招标文件要求格式编制投标文件的 `[来源: 招标文件.pdf p23 视觉OCR]`
- 示例：报价超过最高限价的 `[来源: 招标文件.pdf p45]`

## 七、施工组织设计编制大纲（★ 从 mandatory_outline.json 1:1 翻译，禁止概括/合并/省略）

**数据源：** `extracted/mandatory_outline.json`
**写法：** 若 `JSON.found = true`，把 `JSON.topics` 按 index 顺序逐条翻成主题表，**逐字抄 title_original**（哪怕原文有错别字也保留）；`subsections_original` 翻入"关键子节"列；`attachments` 翻入"附图及附表"清单。**禁止合并、改写、补充原文没有的主题。**

若 `JSON.found = false`，本节填 `JSON.fallback_reason`（通常是"招标文件未明确规定..."），章节锚点回落评分表/通用结构。

**R1 + R5 防御：** 如果你觉得某项主题（如"其他"）"似乎可省略"——这是评分制思维带入合格制项目的典型失败。bid_plan_verify.py 会硬失败拦截：brain.md topics 数 < JSON topics 数 → 立即报警。

**关键词命中（已在 bid_skeleton_extract 阶段处理，此处仅作参考）：**
- "施工组织设计编制要求 / 编制大纲"
- "施工组织总设计 / 施组编制"
- "主要技术内容"
- "技术标准和要求 / 技术标准与要求"
- "技术规范书 / 技术规范"
- "投标文件构成 / 投标文件组成"
- "施工组织设计应包括 / 应包含"

找到后**原文抄录**（不自我概括、不增删条目），并在末尾用 `[来源: 文件名 pXX]` 标注出处。

### 必含一级主题（按招标文件原文顺序）

| 序号 | 主题（原文） | 关键子节/附表（原文） |
|------|-------------|---------------------|
| 1 | ... | ... |

### 附图及附表（若原文规定）

- 附表一：...
- 附图一：...

### 兜底说明

> 若招标文件中确实**未明确规定大纲结构**（小型急单 / 工程量清单类项目常见），本节填：
>
> "招标文件未规定施组编制大纲，章节锚点回落到 GB/T 50502《建筑施工组织设计规范》通用结构 + 评分表（若有）。"
>
> 此情况下 `disqualification_rules.md` 的 `must_include_sections.sections` 字段填空数组 `[]`。
```

#### 2.2 `$ARGUMENTS/extracted/chapter_plan.md`（★ 章节规划表 — 三圈分配）

**章节层面规则：**
- 章节**数量**和**顺序**：
  - 评分制项目 → 评分表条目顺序为主，大纲未覆盖项作为补漏章节追加在评分表对应位置之后
  - 合格制项目 → 大纲一级主题顺序为主章节锚点
  - **任一来源（评分表 OR 大纲）的条目都不得遗漏**，可合并同义条目但不得整条丢失
- 章节**标题**字面复制评分表/大纲原文（不自造、不概括）
- 章节编号从 `01` 开始递增，补零两位
- 评分表/大纲某条若是形式检查（如"装订要求""是否有目录"），不生成章节，标注"格式项，不出章节"
- **每个评分要点/大纲二级节必须展开为子项，不得合并或省略**（子项边界模糊的章节同样要响应每个要点，否则可能丢分）
- 大纲规定的**附图附表**（劳动力计划表、施工设备表、施工总平面图、进度网络图等）必须在对应章节内以表格/图占位形式呈现，不得遗漏

**三圈字数/页数分配：**

第一圈【总量预算，Claude 判档 + 用户确认】
- 依据：合同金额、工期、工程类型、评分表复杂度，判项目规模（小/中/大）
- 参考档位：小项目 150-180 页；中项目 200-240 页；大项目 260-290 页（招标硬顶 300 页）
- 每页字数按招标文件**字体字号行距**查 **Word 实测密度表**（不要用满版理论值，历史教训：满版扣 15% 仍偏高 30%，导致目标 47K 字写完 53K 还超页）：
  - 宋体小五 10pt + 1.5 倍 → **1100-1300 字/页**
  - 宋体五号 11pt + 1.5 倍 → **950-1150 字/页**
  - 宋体小四 12pt + 1.5 倍 → **850-950 字/页**
  - 宋体四号 14pt + 1.5 倍 → **400-450 字/页**（西藏简易综合评估法最常用；全文档有效密度=总字数÷Word总页数，标题/表格/图/封面目录都吃页不吃字。聂荣项目实测 415，旧值 550-700 是纯正文密度导致 50 页目标实出 69 页——禁止再用 625 估算）
  - 仿宋三号 16pt + 固定 28 磅 → **700-800 字/页**
  - 宋体三号 16pt + 1.5 倍 → **450-580 字/页**
  - 计算时取**区间中位值**做目标，按上限做"超页风险底线"
  - 自动查表：`python $BID_GEN_HOME/scripts/bid_derive.py --recommend-chars --pages 70 --font 14 --line 1.5`（也可直接 import `bid_derive.recommend_target_chars`）
- 目标总字数 = 目标文字页数 × 字数密度中位值，图表页数按工程类型估（施工方案密集型 25-30%，制度密集型 15-20%）

第二圈【按分值比例分大章节】
- 章节字数 = 目标总字数 × (章节分值 / 技术标总分)
- 字数带 = 章节字数 × [0.85, 1.15]（15% 浮动）
- 章节图表数按内容估（施工方案/总平面/进度计划/重难点图多；制度类图少）
- 章节页数估 = (章节字数 / 实际文字密度) + (章节图表数 × 0.4)

第三圈【章节字数按评分要点子项数分配】
- 单子项字数 = 章节字数 / 子项数（同一章内平均，不按子项权重再切）
- 每个评分要点形成一个三级子节 `### X.Y 子项标题`
- 子项字数带同 15% 浮动

**两轮阻断：**
- 第一轮：**先只生成"总量预算"段落**（判档理由 + 建议页数 + 字数密度推算），停下等用户确认或调整页数
- 第二轮：用户 confirm 后，才展开大章节表和子项清单

**格式：**

```markdown
# 章节规划表

## 总量预算（第一圈）

**项目规模判档：** 中项目
**判档依据：** 合同金额 3,280 万元 + 工期 180 日历天 + 房建类 + 评分项 10 项
**建议目标页数：** 220 页（300 页上限的 ~73%）
**字数密度推算（Word 实测口径，见 `bid_derive.chars_per_page`）：**
- 招标要求：A4 / 宋体四号 14pt / 1.5 倍行距 / 页边距上 2.5cm 其余 2cm
- 查表：14pt + 1.5 倍 → **400-450 字/页**（取中位 **425 字/页**，全文档有效密度）
- 图表占比估 **25%**（房建施工方案图表多） → 图表页 55 页，文字页 165 页
**目标总字数：** 165 × 425 ≈ **70,125 字**（保守区间 66K-74K，超页风险底线 ≤ 文字页×上限 450）

（★ 等用户确认目标页数后再生成下面两圈）

## 大章节分配（第二圈）

| 章节号 | 文件名 | 章节标题 | 对应评分项 | 分值 | 子项数 | 目标字数 | 字数带 ±15% | 图表数 | 页数估 |
|---|---|---|---|---|---|---|---|---|---|
| 01 | 01_xxx.md | xxx | 第1项 | 15 | 4 | 15,469 | 13,148-17,789 | 8 | 28 |
| 02 | ... | ... | ... | 20 | 3 | 20,625 | ... | 12 | 38 |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |
| **合计** | | | | **100** | | **103,125** | | **55** | **220** |

## 子项分配（第三圈）

### 01 xxx（15,469 字，8 图表，28 页）
- 1.1 评分要点A：3,867 字
- 1.2 评分要点B：3,867 字
- 1.3 评分要点C：3,867 字
- 1.4 评分要点D：3,868 字

### 02 xxx（20,625 字，12 图表，38 页）
- 2.1 ...
...

**总分核对：** 各章分值之和 = 技术标总分 100 分 ✓
**页数核对：** Σ(字数/425) + Σ(图表×0.85) = 220 页 ≤ 招标硬顶 300 页 ✓（425 = 14pt+1.5倍 Word 实测全文档有效密度中位值；渲染图每张≈0.85-1页）
```

#### 2.3 `$ARGUMENTS/extracted/disqualification_rules.md`（废标条款 + 格式参数 + 章节锚点）

结构包含：基本参数、暗标设置、排版格式硬约束、字数密度、否决投标关键节点、关键工期节点、**`must_include_sections`（章节锚点）**。供 `bid_assemble.py` 读排版格式，供 `bid_compliance.py` 读章节锚点。

**`must_include_sections` 字段规范（新增，由 brain 第七节同步）：**

```yaml
must_include_sections:
  source: ""           # brain 第七节的来源标注（招标文件章节定位）
  enforcement: "实质性响应要求，缺一即视为不响应"
  sections:            # 一级主题字符串数组，顺序与 brain 一致
    - 工程概况
    - 编制依据
    - ...（其余主题，从 brain 同步）
  required_attachments:  # 附图附表名称数组（若 brain 列出）
    - 拟投入本标段的主要施工设备表
    - ...
```

**填充规则：**
- 若 brain 第七节有完整大纲 → `sections` / `required_attachments` 从 brain 同步抄录
- 若 brain 第七节为"未明确" → `sections: []` / `required_attachments: []`，章节锚点完全交给评分表驱动
- **不在 skill 提示和脚本中硬编码任何主题清单**，所有内容必须来自具体项目的 brain.md

**★ 暗标范围硬性规定：** 若 `blind_bid: true`，暗标原则（禁身份信息 + 格式要求）适用于 **chapter_plan 列出的全部章节**，无一例外。即使招标文件附表只列出"必须纳入暗标的 N 项内容"，那也是"最低内容要求"，不是"暗标约束范围"。不得在 disqualification_rules.md 或任何提示中写"只有这 N 章走暗标"——否则 writer 在其他章节可能放松警惕，触发 A1.13 级别废标。

#### 2.4 机器校验（★ 在向用户展示前必须跑）

```bash
python3 $BID_GEN_HOME/scripts/bid_plan_verify.py "$ARGUMENTS"
```

检查 8 条铁律（脚本实现全量，文档同步）：
1. **评分总分一致性**：`chapter_plan` 各章分值合计必须等于 `project_brain` 声明的「技术标总分」，不等则退出码 2，阻断流程
2. **废标条款来源标注**：`project_brain`《六、废标条款》每条必须有 `[来源:...]` 标注，未标注退出码 1，须人工复核
3. **章节字数总和一致性**：Σ 章节目标字数 ∈ 目标总字数 × [0.85, 1.15]，不在此范围退出码 1
4. **子项字数与章节字数一致**：每章子项字数之和 ≈ 章节目标字数（±5%），偏差超过退出码 1
5. **总页数 ≤ 硬顶**：估算总页数（Σ字数/每页字数 + Σ图表×0.4）≤ 目标总页数 × 1.05，超则退出码 1
6. **骨架 JSON 存在**：`evaluation_method.json` / `mandatory_outline.json` / `disqualification.json` 三份 JSON 必须存在，缺失退出码 2
7. **brain ↔ 骨架对账**：brain.md 第五/六/七节的字段数量与对应 JSON 一致（如 brain 第七节主题数 = mandatory_outline.json topics 数），不等退出码 2
8. **来源页码集中度检测**：brain.md 第六节 [来源: pXX] 不得过度集中（同页 ≥ 5 条触发警告），集中度异常退出码 1

- 退出码 0：通过，进入 2.5
- 退出码 1：有警告（未标注来源 / 大纲主题缺失 / 页码集中度异常），列给用户确认后再继续
- 退出码 2：硬失败（总分不符 / 骨架缺失 / 对账不一致），修正 brain/plan 后重跑

#### 2.5 派生图表规格（chart_specs.json）

```bash
python3 $BID_GEN_HOME/scripts/bid_chart_specs.py "$ARGUMENTS"
```

工具读 `chapter_plan.md` 各章节图表数 + 子项标题，按关键词启发式给每张图分配 chart type（schedule / org_chart / site_layout / hierarchy_flow / process_flow / resource_curve / aoa_network），并把招标硬性要求的图（总平面图、进度网络图）标 `required:true`，输出到 `extracted/chart_specs.json`。

**用途：**
- bid_writer 在生成章节时把对应 spec 的 schema + 示例注入 prompt，强制 LLM 输出对应 ```json``` 块
- bid_compliance 在 Step 5 比对 chart_specs vs charts/ 实际产出，缺 required 图退出码 1 阻断
- 7 种渲染器的精确 schema 定义在 `$BID_GEN_HOME/scripts/bid_charts.py` `_RENDERER_SCHEMAS`，文档导出在 `$BID_GEN_HOME/docs/chart_schemas.md`

**Claude 介入：**
- 自动派生准确率 ~80%（关键词命中），剩余 20% 用户/Claude 可手动编辑 `chart_specs.json` 改 type 或 required
- 招标只硬要求 5 张附图附表的项目，应把多余的 advisory 图删掉避免过度规划
- 渲染器自检：`python3 $BID_GEN_HOME/scripts/bid_charts_selftest.py` 应该 14/14 PASS

#### 2.6 向用户确认（★ 两轮阻断）

**第一轮：总量预算确认**
仅展示 `chapter_plan.md` 开头的《总量预算》段（第一圈）：
- 项目规模判档 + 依据（合同金额 / 工期 / 工程类型）
- 建议目标页数 + 字数密度推算过程
- 目标总字数 ± 15% 带

阻断等用户回复：
- `/bid-gen confirm pages`（或口头"确认页数"）→ 进入第二轮
- `/bid-gen adjust pages 200`（或"改成 200 页"）→ 用新值重算总字数，再次确认
- 若第一轮不通过，不得展开大章节表和子项清单

**第二轮：章节表与废标条款确认**
用户确认页数后再生成并展示：
- 项目理解摘要（基本信息 + 工程构成 + 难点）
- **`chapter_plan.md` 大章节表**（第二圈：章节标题、分值、子项数、目标字数、图表数、页数估）
- **子项分配清单**（第三圈：每章的评分要点 × 字数）
- 废标条款关键项 + 2.4 校验结果（未标注来源的条款单独列出）
- **chart_specs 派生结果**（2.5 输出）：列出 ★ 招标硬性要求的图 + 其他 advisory 图，请用户确认 type 是否合理、是否要删多余 advisory

**必须等用户明确确认全表后才能进入 Step 3。** 若用户要求调整，修改 chapter_plan.md / chart_specs.json 后再次展示并重跑 2.4 校验。

---

### Step 2.5：批量配置（若为批量投标则执行，单份投标可跳过）

**触发条件：** 用户需要为**多家投标单位**（≥ 2 家）各生成一套技术标。若用户只投一家，跳过此步直接进入 Step 3 的单份模式。

**流程：**
1. 询问用户：**"本次需要为几家投标单位生成技术标？请提供单位名称列表（逗号分隔或逐行列出）。"**
2. 拿到名单后，调用批量配置生成器：

```bash
python $BID_GEN_HOME/scripts/bid_variants.py "$ARGUMENTS" --companies "甲公司,乙公司,丙公司"
```

3. 工具会在 `extracted/variants.json` 写入配置表，每家单位包含以下字段：
   - `id` / `company_name` / `model` / `temperature`
   - 数据指纹：`schedule_offset_days` / `labor_factor` / `equip_delta` / `standard_year_preference`
   - `metadata`：author / last_modified_by / create_time_offset_hours / modified_time_offset_hours
   - `blind`（★ 关键）：由 `disqualification_rules.md` 中的 `blind_bid` 决定，true 时 writer 会把正文中的公司名替换为"本公司"
4. 向用户展示 variants 总表（ID / 公司 / 模型 / 工期偏移 / 劳动力系数 / 设备差异），请用户确认
5. 若用户要求调整，直接编辑 `variants.json` 再确认

> ★ 若 `disqualification_rules.md` 中 `blind_bid: true`，**必须**在 variants.json 每家单位下写入 `"blind": true`，否则 writer 的暗标公司名兜底替换不会触发。

**分配策略（由工具自动处理）：**
- 活跃模型池：GLM-4.6 / DeepSeek-V3.2 / Qwen3-Max / Kimi-K2.5 / MiniMax-M2.5（豆包 ARK_API_KEY 未充值，config 中 `disabled:true` 跳过）
- ≤ 6 家：每家分一个不同模型
- 7~12 家：模型轮转 + 温度组合（0.5 / 0.7 / 0.9）
- 13+ 家：上述 + 数据指纹偏移幅度放大 1.5 倍
- **推理关闭**：GLM/Kimi 通过 `extra_body={"thinking":{"type":"disabled"}}` 强制关深度思考；MiniMax 通过 `reasoning_split:true` + `<think>` 正则兜底剥离；Qwen3-Max/DeepSeek-chat 本身非推理版。目的是节省 8K token 预算避免字数不足
- **温度强制**：Kimi-K2.5 API 仅允许 `temperature=0.6`（k2-thinking 才是 1.0），已在 config `force_temperature` 指定；`_assign_model` 遇到 force_temperature 模型会用强制值覆盖轮转温度

**模型 API 依赖：** 需要在 `~/.env` 配置各家 API Key（`GLM_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `MOONSHOT_API_KEY` / `MINIMAX_API_KEY`；豆包 `ARK_API_KEY` 充值后可把 `bid_llm_config.json` 中 doubao 的 `disabled` 改 false）。可先运行连通性自检：

```bash
python $BID_GEN_HOME/scripts/bid_llm_client.py --test-all
```

---

### Step 3：按 chapter_plan 逐章生成

**两种模式：**

#### 模式 A：单份投标（无 variants.json）

由 Claude 亲自逐章撰写到 `output/` 目录。

**循环规则：** 读取 `chapter_plan.md`，按表中顺序逐行生成章节文件；**不使用任何固定 01~06 模板**。

**每章生成前必须做的事：**
1. 读取 chapter_plan.md 中本章的「对应评分项」「评分要点/档次描述」「目标字数」「字数带 ±15%」「子项分配」
2. 读取 project_brain.md 中与本章主题相关的内容
3. 读取 disqualification_rules.md 检查是否有针对本章的特殊要求
4. **严格按 chapter_plan 的目标字数 ±15% 带写作，图表不计入字数**（不得再用"分值×300"线性估算——那是旧公式，与 Step 2 圈层理论冲突）

**每章结构通用原则：**
- 第一级标题（`#`）= chapter_plan 中的章节标题（字面一致）
- **每个评分要点/档次 = 一个三级子节 `###`，标题采用 chapter_plan 第三圈子项清单中的文字**；边界模糊的要点也不得合并或省略，否则可能被评委按"未响应"扣分
- 正文围绕评分要点实质展开（不是把关键词照抄成标题），每个评分档次描述里的关键词都要被覆盖
- 必须结合本项目的工程构成、重点、难点，不写通用废话
- 引用与项目类型对应的现行国家/行业标准（GB/T、JGJ、CJJ、TD/T、SL等）

**★ 暗标全章节适用（`disqualification_rules.md` 中 `blind_bid: true` 时）：**
- **所有章节**一律禁公司名、LOGO、项目业绩、人员真实姓名、获奖证书编号——无白名单，feedback_bid_blind_scope
- **禁用装饰字符** 【】★◆■●※☆ 等，易被判"做标记"违反暗标；正文需要强调用 `**粗体**` 或阿拉伯数字编号
- **禁用 Markdown 斜体**（`*text*` / `_text_`）：斜体在 docx 里是装饰效果，同样有"做标记"风险；强调一律用 `**粗体**`
- 章节标题编号统一 `01/02/...`（阿拉伯数字），不用特殊符号框装饰

**★ 图表与代码块规范（Step 4 组装阶段会按此处理）：**
- **禁用 Mermaid**：不要输出 ```mermaid ... ``` 代码块；docx 里无法渲染，会被 Step 4 直接剥离
- **JSON 仅限进度计划章**：只有进度计划章节的文末可附 ```json 代码块（必须含 `tasks` / `milestones` 字段），由 Step 4 `bid_charts.py` 自动渲染为横道图 PNG 嵌入 docx；其他章节禁止使用 ```json```
- **进度 JSON 数据结构**：
  ```json
  {"tasks": [{"name":"...","start":"2026-05-01","end":"2026-06-15","phase":"基础"}, ...],
   "milestones": [{"name":"主体封顶","date":"2026-08-20"}, ...]}
  ```
  `start/end` 必须是 `YYYY-MM-DD` 格式，`phase` 可选（影响横道颜色分组）
- **Markdown 表格硬约束**：
  - **列数 ≤ 6 列**；超过 6 列必须按阶段/分项/工种拆为多张小表（7 列以上 docx 会溢出页边距）
  - "说明/备注"列内容 ≤ 15 字；长说明改为表下方的文字段落而非大单元格
  - 表头字段名简洁（2-6 字），避免"主要施工机械设备进场退场计划表"这种把列宽撑爆的长标题
- **禁用 ASCII 艺术/字符画进度图**（如 `████░░░░`）：等宽字符在 Word 里字体一变就错位；有需要请用 Markdown 表格 + 文字描述

#### 模式 B：批量投标（存在 variants.json）

由下游大模型分布式撰写，每家单位的章节落到 `output/V01/`, `output/V02/`, ... 子目录。

```bash
python $BID_GEN_HOME/scripts/bid_writer.py "$ARGUMENTS"
```

**关键点：**
- 工具读取 variants.json 和 chapter_plan.md，为每家 × 每章构造差异化 prompt（注入数据指纹）
- 调用各家单位对应的下游大模型完成撰写
- Claude **不直接写正文**，职责转为**编排 + 后续 Step 3.5 审校**
- 生成完毕后 `output/V01/01_xxx.md`, ... 等文件就绪
- `output/writer_stats.json` 记录各模型 token 用量

**断点续跑：** 若部分 variant 失败，`bid_writer.py --variant V03 --force` 可单独重跑。

**生成完毕后必须进入 Step 3.5 审校**，不可直接跳到 Step 4。

---

### Step 3.5：机器层审校（★ 批量模式必须执行；单份模式可选）

由下游大模型写的章节质量良莠不齐，组装 DOCX 前先做机器审校：

```bash
# 批量模式：审所有 variant
python $BID_GEN_HOME/scripts/bid_reviewer.py "$ARGUMENTS"

# 单份模式
python $BID_GEN_HOME/scripts/bid_reviewer.py "$ARGUMENTS" --single

# 仅审某一家
python $BID_GEN_HOME/scripts/bid_reviewer.py "$ARGUMENTS" --variant V01
```

**检查项：**
1. 字数达标（目标 × 85%）
2. 一级标题与 chapter_plan 字面一致
3. 评分要点关键词覆盖度（< 70% 记 minor）
4. 暗标禁用词 / 公司名违规
5. 严重性分级：ok / minor / moderate / major

**产出：**
- `output/VXX/review.json` + `review.md`（每家一份）
- `output/review_summary.json`（所有 variant 汇总，供 Claude 决策）

**Claude 处理：** 读取 `review_summary.json`，对 **severity=major** 的章节调用 `bid_writer.py --variant VXX --chapter NN --force` 重生成；minor / moderate 由 Claude 判断是否重写，或在 Step 4 组装后再人工复核。

---

**特殊章节附加要求（A/B 模式通用）：**

| 章节主题（若评分表出现） | 附加要求 |
|------------------------|---------|
| 进度计划类 | 文末附 JSON 进度数据块（tasks/milestones），总工期≤招标要求 |
| 资源配置类 | 劳动力+机械+材料三张表，与工程量匹配 |
| 质量措施类 | 各分项逐一对应施工方案中的分项，不漏项 |
| 安全措施类 | 针对本项目实际风险源（高处/基坑/临电/机械等） |
| 环境措施类 | 结合项目所在地环保敏感点 |
| 项目管理机构类 | 组织机构（文字描述优先，图表为后期优化项） |
| 新技术/BIM类 | 具体应用点，不空谈 |

**参考片段库：** 若评分表出现施工方案/进度/质量/安全/环境/资源等常见主题，可参照本文件末尾附录 A 的写作结构（但标题必须用评分表原文）。

---

### Step 4：组装 Word 文档

**单份模式：**

```bash
python $BID_GEN_HOME/scripts/bid_assemble.py "$ARGUMENTS/output/"
```

**批量模式（每家单位一份独立 DOCX）：**

```bash
python $BID_GEN_HOME/scripts/bid_assemble.py "$ARGUMENTS/output/" --batch
```

批量模式会：
- 扫描 `output/V*/` 每个子目录，生成 `V01_甲公司_xxx_施工组织设计.docx` 等
- 按 variants.json 注入每家独立的 DOCX 元数据（creator / lastModifiedBy / created / modified）
- 清空修订历史与元评论，避免元数据层雷同

---

### Step 5：废标合规检查（★ 必须执行，不可跳过）

**单份模式：**

```bash
python $BID_GEN_HOME/scripts/bid_compliance.py "$ARGUMENTS"
```

**批量模式（逐家扫描）：**

```bash
python $BID_GEN_HOME/scripts/bid_compliance.py "$ARGUMENTS" --all-variants
# 或仅扫某一家
python $BID_GEN_HOME/scripts/bid_compliance.py "$ARGUMENTS" --variant V01
```

批量模式会在每个 `output/VXX/` 下生成独立 `compliance_scan.json` + `compliance_scan.md`。

生成 `compliance_scan.json` 和 `compliance_scan.md`，包含：
1. 暗标禁用信息扫描
2. 关键响应词检查
3. 章节字数统计
4. **章节-评分表对齐核查**（新增）：章节文件是否覆盖 chapter_plan 的每一项，标题是否字面一致

读取：
- `extracted/disqualification_rules.md`
- `extracted/chapter_plan.md`
- `output/compliance_scan.json`
- `output/` 下所有章节 .md

**逐条核查并输出 `$ARGUMENTS/output/合规检查报告.md`：**

```markdown
# 废标合规检查报告

## 检查结论
总体状态：✅ / ⚠️ / ❌

## 一、章节-评分表对齐核查（最高优先级）
| 评分项 | 要求章节标题 | 实际章节文件 | 标题一致 | 字数达标 | 说明 |
|--------|------------|------------|---------|---------|------|
| 1 | xxx | 01_xxx.md | ✅ | ✅ (4520字) | |

## 二、内容类废标核查
| 检查项 | 要求 | 结果 | 说明 |

## 三、格式类废标核查
| 检查项 | 要求 | 结果 | 说明 |

## 四、风险项处理
```

**❌ 项处理：** 立即修复 .md → 重跑 Step 4 → 重跑 Step 5。
**⚠️ 项处理：** 向用户说明，由用户决策。

**最终输出（单份模式）：**
- ✅ `extracted/project_brain.md`
- ✅ `extracted/chapter_plan.md`
- ✅ `extracted/disqualification_rules.md`
- ✅ `output/0X_*.md`（按 chapter_plan 数量）
- ✅ `output/*.docx`
- ✅ `output/合规检查报告.md`

**最终输出（批量模式）：**
- ✅ 上述 extracted/ 全部文件
- ✅ `extracted/variants.json`
- ✅ `output/VXX/0X_*.md`（每家一套章节）
- ✅ `output/VXX/review.json` + `review.md`（每家审校）
- ✅ `output/VXX/compliance_scan.json` + `compliance_scan.md`（每家合规）
- ✅ `output/V01_公司A_xxx_施工组织设计.docx` ... 每家一份独立 DOCX
- ✅ `output/review_summary.json`（审校汇总）
- ✅ `output/similarity_matrix.json` + `相似度检查报告.md`（见 Step 6）
- ✅ `output/writer_stats.json`（模型 token 用量）

向用户报告：生成章节数、总字数、各章分值覆盖情况、风险项。批量模式额外报告：variant 数、审校严重性分布、相似度超阈值对数。

---

### Step 6：交叉相似度检查（★ 仅批量模式，必须执行）

批量投标的核心风险是被评标委员会判定"围标/串标"。同一项目的多家技术标一旦在**整体重复率 ≥ 40%** 或 **段落重复率 ≥ 50%** 就高危。

```bash
python $BID_GEN_HOME/scripts/bid_similarity.py "$ARGUMENTS"
# 可覆盖阈值
python $BID_GEN_HOME/scripts/bid_similarity.py "$ARGUMENTS" --threshold-overall 0.4 --threshold-paragraph 0.5
```

**检查维度：**
1. 整体文字重复率（SimHash + jieba，降级到 difflib）
2. 段落级最大重复率（n-gram Jaccard）
3. 数据指纹重复率（数字集合 Jaccard — 验证各家工期/数量/系数是否真差异化了）
4. DOCX 元数据独立性（author / last_modified_by / created）

**产出：**
- `output/similarity_matrix.json`（完整矩阵）
- `output/相似度检查报告.md`（人读报告 + 超阈值对 + 重写建议）

**Claude 处理：**
- 若存在超阈值对，报告中会给出具体章节重写命令，如：
  ```bash
  python $BID_GEN_HOME/scripts/bid_writer.py "$ARGUMENTS" --variant V03 --chapter 02 --force
  ```
  执行后回到 Step 3.5 重新审校 → Step 4 重新组装 → 再次 Step 6，直到全部在阈值内。
- 元数据冲突（author 或 created 相同）也要提示用户重跑 `bid_assemble.py --batch`。

---

---

### Step 6.5：评标专家评审（★ 自动执行，只出报告不阻断）

similarity 通过后、summary 之前，自动以"评标专家"视角对最终方案做一次模拟评审。区别于 Step 3.5（机器规则审校）与 Step 5（合规红线），这一步是**对照招标评审标准的语义级打分**——模拟真人评委找扣分点。

```bash
python $BID_GEN_HOME/scripts/bid_expert_review.py "$ARGUMENTS"
python $BID_GEN_HOME/scripts/bid_expert_review.py "$ARGUMENTS" --variant V01   # 只评一家
python $BID_GEN_HOME/scripts/bid_expert_review.py "$ARGUMENTS" --model deepseek # 默认 deepseek
```

**检查维度：**
1. 逐项瑕疵制打分（LLM 评标专家 persona）：读 `evaluation_method.json` 的 `scoring_items`，按章节同序映射，按招标原文阶梯（0 瑕疵满分 → ≥4 瑕疵 0 分）给每项打分
2. 符合性/废标风险（规则）：占位符/写手指令泄漏、规范年份伪造、**金额失实**（正文安全文明施工费/暂列金额 vs 清单真值）、总工期矛盾
3. 跨章数据一致性（LLM）：同一参数跨章是否打架（尺寸/规格/吨位/材料/工期节点）

**产出：**
- `output/VXX/expert_review.json` + `expert_review.md`（每家逐项打分 + 瑕疵清单）
- `output/expert_review_summary.json`（排名）
- `output/reports/评标专家评分报告.md`（最终交付报告，带原文锚点）

**Claude 处理：** 此步**不阻断、不回 writer**（退出码恒 0）。报告用于交付前自查——若发现致命瑕疵（伪造痕迹/金额失实/材料体系矛盾），回到相应章节 `bid_writer.py --force` 修复后重新组装 + 重跑本步。已知局限：LLM 对"量化虚胖"（如材料吨位超控制价）不够敏感，重大数值疑点建议人工再核。

---

## 附录 A：常见主题写作结构参考（仅参考，标题必须用评分表原文）

### 施工方案/施工组织设计类
- 工程概况与施工条件分析
- 施工总体部署（组织机构、段落划分、施工顺序）
- 各分项工程施工方案（逐一展开，核心）
- 关键技术措施（针对难点的专项方案）

### 进度计划类
- 编制依据与原则
- 施工阶段划分
- 各分项进度安排与关键线路
- 进度保证措施（组织/技术/经济/合同）
- 季节性施工措施
- 文末 JSON 进度数据

### 质量措施类
- 质量管理体系与目标
- 各分项质量控制要点
- 关键部位专项质量措施
- 质量通病防治
- 成品保护

### 安全措施类
- 安全管理体系与目标
- 危险源辨识与控制
- 各分项安全操作要点
- 文明施工
- 应急预案

### 环境措施类
- 环保目标
- 扬尘/噪声/污水/固废分项措施
- 水土保持（若适用）

### 资源配置类
- 劳动力配置（各工种各阶段人数表）
- 主要机械设备（型号/数量/进场时间表）
- 主要材料供应计划
