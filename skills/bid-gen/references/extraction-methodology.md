# 招标文件信息提取方法论 v2

> 适用于 `/bid-gen` 流程的 Step 1 → Step 2 阶段。本文档替代旧版「6 个 prompt 模板 + 7 类 schema」方法论。
>
> 核心思想:**章节锚点骨架(评分办法/必含大纲/废标条款)由机器先抽 JSON,再让 Claude 把 JSON 翻译成 brain.md。Claude 不再凭印象重写骨架,只能翻译。**

---

## 0. 为什么有 v2

旧方法论建议"按用途扫,7 类信息"。这导致三个真实项目(仓储类项目A/学校类/仓储类项目B)出现以下系统性失败:

| 失败模式 | 证据 | 影响 |
|---------|------|------|
| **R1**:招标第七章「主要技术内容 16 项」必含大纲被漏抄 | 仓储类项目A brain.md 完全没第七节;食堂 brain.md 完全没第七节;仓储类项目B brain.md 第七节是返工补的且最初漏第 16 项 | chapter_plan 没有源头依据,附图错放章节、章节缺失等连锁错误 |
| **R4**:废标条款来源页号集中度异常 | 仓储类项目A brain.md 14 条废标**全部标 [来源: ... p75]**——不可能 14 条都在同一页 | 来源标注流于形式,无法支撑回溯 |
| **R5**:评分制思维被带入合格制项目 | 仓储类项目B项目把"其他"判为可省略,导致 16 项缩成 15 章 | 同一类错误反复出现 |
| **R6**:附图附表归属在 chapter_plan 阶段判错 | 仓储类项目A把附表四/五错放第 6 章而非第 15 章 | brain → plan 单向写作,plan 无 brain 背书时凭通用经验排 |

根因抽象到三个层次:

- **L1 知识结构**:把「评分表/必含大纲/废标条款」当成扁平 7 类之一,实际它们是**结构性骨架**,权重应高于其余 4 类细节属性。
- **L2 流程闭环**:`brain → chapter_plan → rules` 是单向链。"机器抽到的关键页"和"Claude 写的 brain"之间没有 diff 校验。
- **L3 元认知**:Step 2 自由提取时 Claude 没有强制确认"我读完原文了吗"。

v2 的解决方案:**强制把骨架抽取交给机器(LLM 出 JSON),让 Claude 只做"翻译"和"细节填充",最后用机器校验对账。**

---

## 1. 四阶段强制闭环

```
                ┌──────────────────────────────────────────────┐
                │  阶段 A:文件分类(全机器)                       │
                │   bid_extract.py + bid_fingerprint.py        │
                │   产出:extracted/*.txt + manifest.txt +       │
                │         vision_report.json + fingerprints.json│
                └──────────────────────────────────────────────┘
                                    │
                                    ▼
                ┌──────────────────────────────────────────────┐
                │  阶段 B:骨架优先提取(机器 LLM)                 │
                │   bid_skeleton_extract.py                    │
                │   产出三份 JSON:                              │
                │   • evaluation_method.json                   │
                │   • mandatory_outline.json                   │
                │   • disqualification.json                    │
                │   产出 document_map.md(章节-页码索引)         │
                └──────────────────────────────────────────────┘
                                    │
                                    ▼
                ┌──────────────────────────────────────────────┐
                │  阶段 C:细节填充(Claude)                      │
                │   读三份 JSON 作为骨架                         │
                │   写 project_brain.md(第 5/6/7 节直接基于     │
                │       JSON 翻译,不得重写;1-4 节正常生成)      │
                │   写 chapter_plan.md(章节锚点 = JSON 并集)    │
                │   写 disqualification_rules.md(format + 锚点) │
                └──────────────────────────────────────────────┘
                                    │
                                    ▼
                ┌──────────────────────────────────────────────┐
                │  阶段 D:闭环校验(全机器)                       │
                │   bid_plan_verify.py                         │
                │   失败 → 退出码非 0 → 阻断 Step 3             │
                └──────────────────────────────────────────────┘
```

每个阶段必须完成后才能进入下一阶段。**没有"懒人套餐"。** 阶段 B 失败时 Claude 不得绕过去自由提取——这是 v2 与旧方法论最大的区别。

---

## 2. 阶段 A:文件分类(必跑)

### 2.1 命令

```bash
python Claude Code 脚本目录/bid_extract.py "$TENDER_DIR"
python Claude Code 脚本目录/bid_fingerprint.py "$TENDER_DIR"  # 推荐
```

### 2.2 关键产出

- `extracted/*.txt`:每个文件提取后的纯文本,含 `[第N页]` 段头
- `extracted/manifest.txt`:文件清单 + 视觉 OCR 触发统计
- `extracted/vision_report.json`:每个 PDF 的视觉 OCR 详细报告(每页类别、模型、命中关键词)
- `extracted/fingerprints.json`:每个 PDF 的页数/纸张/启发式初判(可选)

### 2.3 视觉 OCR 强制触发的关键词

下列 4 类关键词命中即强制走视觉模型(即便 pdfplumber 抽到了字),保证表格结构和原文忠实度:

- `disqualify`:废标/否决条款
- `blind_format`:暗标 + 排版 + 装订
- `scoring`:评分办法
- `outline`:**施组编制大纲(★ v2 新增)**

`outline` 类的关键词在 `Claude Code 配置目录/bid_vision_config.json`,包括"主要技术内容""技术标准和要求""施工组织设计编制要求"等。这是 v2 修复 R1 根因的第一道闸——保证招标第七章 16 项原文一定走视觉重抽,不被 pdfplumber 漏行。

### 2.4 完成判据

- `extracted/manifest.txt` 存在
- 至少有一个 `[视觉OCR:...]` 标记的页(如果招标文件主体确实命中关键词)
- 主文档识别可由阶段 B 自动完成,本阶段无需人工干预

---

## 3. 阶段 B:骨架优先提取(必跑,LLM 层)

### 3.1 命令

```bash
python Claude Code 脚本目录/bid_skeleton_extract.py "$TENDER_DIR"
```

或在 bid_run.py 流程内自动跑(已嵌入 Step 1.5)。

### 3.2 三份 JSON 的契约 schema

#### 3.2.1 `evaluation_method.json`(B1 评标办法)

```json
{
  "evaluation_method": "score | passfail",
  "method_chinese": "综合评估法 / 简易综合评估法 / ...",
  "total_tech_score": 0,
  "tech_score_is_passfail": true,
  "scoring_items": [
    {
      "id": "1.1",
      "title_original": "投标总报价",
      "score": 24,
      "criteria_original": "评标基准价法...",
      "source_page": 78,
      "source_page_method": "ocr | text"
    }
  ],
  "veto_items": [
    {
      "text_original": "投标报价超过控制价的",
      "source_page": 21,
      "source_page_method": "ocr | text"
    }
  ],
  "_meta": {"source_file": "...", "total_pages": 222, "model": "glm"}
}
```

**判定规则(关键)**:

- 当施工组织设计在评标表中**只评合格/不合格、不计分**时,`evaluation_method = "passfail"`,
  `total_tech_score = 0`,`tech_score_is_passfail = true`。`scoring_items` 仍要列出
  商务标各项(报价/信用/业绩等),不留空。
- 否则 `evaluation_method = "score"`,`total_tech_score` 取技术标实际分值。
- **每条 source_page 必须来自原文 [第N页] 标记**。

#### 3.2.2 `mandatory_outline.json`(B2 必含大纲)

```json
{
  "found": true,
  "source_chapter": "第七章 技术标准和要求",
  "source_page_start": 149,
  "source_page_end": 159,
  "source_page_method": "ocr",
  "topics": [
    {
      "index": 1,
      "title_original": "工程概况",
      "subsections_original": ["项目主要情况", "施工条件"],
      "notes": null
    }
  ],
  "attachments": [
    {"label": "附表一", "title_original": "拟投入本标段的主要施工设备表"}
  ],
  "fallback_reason": null,
  "_meta": {...}
}
```

**判定规则**:

- 在原文中按 11 个关键词查找:`施工组织设计编制要求/编制大纲`、`施工组织总设计`、
  `施组编制`、`主要技术内容`、`技术标准和要求`、`技术规范书`、`技术规范`、
  `投标文件构成/组成`、`施工组织设计应包括/应包含`、`施工组织设计内容`。
- `topics` 必须**按原文顺序**,**逐字抄录**(不概括/合并/省略)。
- 招标确实没规定大纲(小型急单常见)时 `found = false`,
  `fallback_reason = "招标文件未明确规定..."`。

#### 3.2.3 `disqualification.json`(B3 废标条款全表)

```json
{
  "clauses": [
    {
      "category": "报价 | 资格 | 形式 | 格式 | 评审 | 其他",
      "text_original": "投标报价超过控制价的",
      "consequence": "否决投标 | 废标 | 无效 | ...",
      "source_file_hint": "B1.10",
      "source_page": 75,
      "source_page_method": "ocr"
    }
  ],
  "summary": {"total_count": 24, "by_category": {...}},
  "_meta": {...}
}
```

**判定规则**:

- 不漏一条。即便条款写得罗嗦也拆为独立条目。
- **同一页 ≥ 5 条 → bid_skeleton_extract 内部校验报警**(R4 根因防御)。
- `consequence` 按原文真实选词:有的项目"按无效投标处理",有的"否决投标",
  按原文抄,不统一改写。

### 3.3 退出码语义

| 退出码 | 含义 | bid_run.py 的处理 |
|-------|------|------------------|
| 0 | 三份 JSON 全部生成且通过自检 | 进入阶段 C |
| 1 | 软警告(如 outline `found=false`,或同页集中度警告) | 进入阶段 C,警告列给用户 |
| 2 | 硬失败(主文档识别失败 / LLM 全部不可用 / JSON 反复解析失败) | 阻断;但 bid_run.py 当前降级为继续(让 Claude 在阶段 C 手工补齐) |

### 3.4 LLM 的注意事项

- 默认使用 `bid_llm_config.json` 的 `default_model`(通常 GLM-4.6)
- prompt 强制低温(0.2),要求只输出 JSON
- 三个任务并行调用(ThreadPoolExecutor),provider 内部有 semaphore 限流
- 主文档全文长度通常 100K-300K 字符(中文 token 约 50K-150K),GLM/DeepSeek 128K 上下文够用
- 失败时可 `--only b2` 重跑某一项,`--force` 覆盖已有 JSON

---

## 4. 阶段 C:细节填充(Claude 层,核心规则)

### 4.1 阶段 C 的角色定位

> **Claude 在阶段 C 的工作不是"提取信息",而是"把机器抽好的 JSON 翻译成 markdown"。**
> 重写骨架 = 流程退化为 v1 的失败模式,机器校验会拦截。

### 4.2 写 `project_brain.md`

| 节 | 数据来源 | 是否允许 Claude 重写 |
|---|---------|------------------|
| 一、项目基本信息 | 招标文件 + 工程量清单 + 控制价文件 | 是,Claude 自由提取 |
| 二、项目类型与工程构成 | 招标文件 + 工程量清单 + 技术规范书 + CAD | 是,Claude 自由提取(应深度交叉到附件) |
| 三、技术重点与施工难点 | 招标文件 + 技术规范书 + 地勘报告 + 当地条件 | 是,Claude 自由分析 |
| 四、施工约束条件 | 招标文件 + 技术规范书 + 合同范本 | 是,Claude 自由提取 |
| **五、技术标评分表** | **`evaluation_method.json`** | **否,只能 1:1 翻译** |
| **六、废标条款** | **`disqualification.json`** | **否,只能 1:1 翻译,每条 source_page 必须来自 JSON** |
| **七、施工组织设计编制大纲** | **`mandatory_outline.json`** | **否,topics 数量必须 = JSON,顺序必须一致** |

第 5/6/7 节的写法:

```markdown
## 五、技术标评分表

> 数据来源:`extracted/evaluation_method.json` (extracted by bid_skeleton_extract)

评标方法:简易综合评估法
技术标总分:0(施工组织设计采用合格制,不计分)

| 序号 | 评分项标题(原文) | 分值 | 评分要点 |
|---|---|---|---|
| 1.1 | 投标总报价 | 24 | 评标基准价法... |
| ... | ... | ... | ... |

(逐行照抄 evaluation_method.json.scoring_items 字段,顺序保留)
```

```markdown
## 六、废标条款

> 数据来源:`extracted/disqualification.json` (24 条,5 个类别)

### 报价类
- 投标报价超过控制价 `[来源: 招标文件.pdf p21]`
- ...

### 资格类
- ...
```

```markdown
## 七、施工组织设计编制大纲

> 数据来源:`extracted/mandatory_outline.json`
> 来源章节:第七章 技术标准和要求 p149-159

### 必含一级主题(按招标文件原文顺序)

| 序号 | 主题(原文) | 关键子节/附表(原文) |
|------|-------------|---------------------|
| 1 | 工程概况 | 项目主要情况 / 施工条件 |
| 2 | 编制依据 | 法律法规 / 国家行业地方标准 / ... |
| ... | ... | ... |

### 附图及附表

- 附表一:拟投入本标段的主要施工设备表
- 附表四:计划开、竣工日期和施工进度网络图
- 附表五:施工总平面图
```

### 4.3 写 `chapter_plan.md`

**章节锚点 = `mandatory_outline.json.topics` ∪ `evaluation_method.json.scoring_items` 的并集去重。**

- 合格制项目(`evaluation_method = "passfail"`):章节锚点 = 必含大纲 topics(主)
- 评分制项目(`evaluation_method = "score"`):章节锚点 = 评分表条目(主) + 大纲 topics(补漏)
- 两者都缺失:回落 GB/T 50502《建筑施工组织设计规范》通用结构

### 4.4 写 `disqualification_rules.md`

`must_include_sections.sections` 字段直接从 `mandatory_outline.json.topics` 同步,
**禁止硬编码任何主题清单**。

### 4.5 暗标范围(R-blind 防御)

若 `disqualification_rules.md` 中 `blind_bid: true`,暗标约束适用于
**chapter_plan 列出的全部章节**,无白名单。即便招标附表只列了"必须纳入暗标的 N 项",
那是"最低内容要求"不是"暗标约束范围"。`variants.json` 每家单位下必须写 `"blind": true`。

### 4.6 附图附表归属(R6 防御)

招标第 N 章「附图及附表」规定的附表(进度网络图/施工总平面图等),
**必须集中放在该章对应的 chapter_plan 章节**,正文章节只能文字 + 跳转引用。
不得把附表四/五错放进度计划章或施工平面布置章。

---

## 5. 阶段 D:闭环校验(必跑)

### 5.1 命令

```bash
python Claude Code 脚本目录/bid_plan_verify.py "$TENDER_DIR"
```

### 5.2 八条铁律(v2 共 8 项,新增 5/6/7/8)

| # | 校验项 | 失败 → 退出码 |
|---|-------|------------|
| 1 | 评分总分一致性:Σ chapter_plan 分值 = brain 声明 | 2 |
| 2 | 三圈字数:Σ 章节字数 ∈ 总预算 ±15% | 2 |
| 3 | 子项覆盖:每章子项合计 ≈ 章节目标 ±5% | 1 |
| 4 | 总页数硬顶:估算页数 ≤ 招标硬顶 ×1.05 | 2 |
| 5 | **必含大纲覆盖**:mandatory_outline.topics ⊆ chapter_plan(语义) | 1 |
| 6 | **来源页码集中度**:brain 第六节同页 ≥ 5 条 → 警告;同页 100% → 硬失败 | 1 / 2 |
| 7 | **骨架 JSON 存在性**:三份 JSON 必须存在且 _meta 有效 | 1 |
| 8 | **brain↔skeleton 对账**:第五节行数 ≈ JSON 评分项数;第六节 ≥ JSON 50%;第七节 = JSON topics | 1 / 2 |

第 7 节缺失但 JSON 存在 = **硬失败**(R1 根因复现):

```
✗ 骨架抓到 16 个必含主题,但 brain.md 第七节未抄录(R1 根因复现)
```

### 5.3 失败处理

退出码 2 → Claude 必须修复 brain/plan/rules 后重跑,不得绕过。
退出码 1 → 警告列给用户,人工确认后可继续。

---

## 6. 历史失败案例索引(回归测试)

新方法论必须能阻止下列三个项目历史上犯过的具体错误:

### 6.1 仓储类项目A(R1 + R4 双重)

- brain.md 缺第七节大纲 → 阶段 D 第 7/8 条硬失败(JSON 存在但 brain 没抄)
- 14 条废标全部 [来源: p75] → 阶段 B B3 自检警告 + 阶段 D 第 6 条警告

### 6.2 学校类项目(R1)

- brain.md 缺第七节(评分制项目下也漏抄)→ 阶段 D 第 7/8 条硬失败
- 食堂项目曾经 chart_specs.json 未生成(静默降级)→ 阶段 D 不直接管,
  但 bid_run.py state.json 会有 `chart_specs: skip` 记录

### 6.3 仓储类项目B(R5 + R6)

- brain 第七节漏第 16 项「其他」 → 阶段 B B2 输出 16 条;阶段 C Claude 翻译时若
  漏抄,阶段 D 第 8 条警告(brain topics 数 < JSON topics 数)
- 附表四/五错放第 6 章 → 阶段 D 第 5 条覆盖核验:第 15 章「附图及附表」必含主题
  在 chapter_plan 中找不到对应,警告

---

## 7. Claude 在 Step 2 的具体动作清单

```
1. [ ] 读 extracted/manifest.txt,确认主文档识别
2. [ ] 读 extracted/document_map.md,确认章节-页码索引
3. [ ] 读 extracted/evaluation_method.json,理解评标方法 + 否决项
4. [ ] 读 extracted/mandatory_outline.json,理解必含大纲(found 状态 + topics)
5. [ ] 读 extracted/disqualification.json,理解全部废标条款
6. [ ] 写 project_brain.md
       一-四节:Claude 自由组织,可结合附件深度交叉
       五-七节:从三份 JSON 1:1 翻译,不重写
7. [ ] 写 chapter_plan.md
       章节锚点 = JSON 并集去重
       第一圈/第二圈/第三圈三层预算
8. [ ] 写 disqualification_rules.md
       must_include_sections.sections 从 JSON 同步,不硬编码
9. [ ] 跑 bid_plan_verify.py
       退出码必须 ≤ 1 才能进 Step 3
10.[ ] 与用户确认两轮:总量预算 + 章节表
```

---

## 8. 与 v1 的对照

| 维度 | v1 | v2 |
|------|----|----|
| 提取方式 | 6 个 prompt 模板让 Claude 自由扫 | 阶段 B 机器出 JSON,阶段 C Claude 翻译 |
| 信息分类 | 7 类扁平(生死/决策/评分/格式/内容/商务/流程) | 骨架(评分/大纲/废标)优先,细节其次 |
| 来源标注 | 要求 [来源: 文件名 pXX] | 同 v1 + 集中度校验(R4 防御) |
| 必含大纲 | "Prompt 6 反向提取(评分制专用)" | 独立强制 prompt B2,合格制 + 评分制都跑 |
| 校验机制 | bid_plan_verify(总分 + 来源标注) | bid_plan_verify(8 条铁律,含 brain↔JSON 对账) |
| 失败回收 | 用户口头确认旁路 | 退出码非 0 阻断,无旁路 |

---

## 9. 维护

- 三份 JSON schema 变更必须同步:`bid_skeleton_extract.py` + `bid_plan_verify.py` + 本文档第 3.2 节
- `bid_vision_config.json` 关键词调整时,outline 类的 11 个关键词必须保持完备
- 新增视觉 OCR 模型时,`source_page_method` 字段值域需对应扩展
- 历史失败案例(第 6 节)是回归测试基准,新加项目暴露新模式时应补到这里
