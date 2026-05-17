# bid-gen 系统优化方案

> 分析日期：2026-05-15  
> 分析范围：全部 29 个脚本（26 + 3 合成）+ 1 个主提示词（696 行）+ 19 条记忆 + 3 份配置  
> 参考实例：民房类项目（合格制，70 页硬顶）

---

## 总览

系统整体设计扎实，v2 骨架优先方法论解决了历史 R1-R6 系统性失败。核心问题是**自动化程度不足**——Step 2 是整条管道的瓶颈，而系统中已有 5 个脚本可以自动化它的 80%，却未被主工作流集成。

以下按影响程度从高到低排列 10 项优化建议。

---

## 优化一：Step 2 完整自动化（影响最大）

### 现状

`commands/bid-gen.md` 明确要求 Step 2 由 LLM 手工完成三个文件（brain / plan / rules），这是一个严重的产能瓶颈。但 `scripts/` 下实际存在以下可自动化 Step 2 的脚本：

| 脚本 | 行数 | 功能 | 主提示词是否引用 |
|------|------|------|:---:|
| `bid_brain_translate.py` | 423 | 从 JSON 自动生成 project_brain.md（7 节完整） | ✗ |
| `bid_difficulty_synthesize.py` | 245 | 调 LLM 生成 difficulties.json（重点/难点/约束）供 brain 第 3/4 节 | ✗ |
| `bid_plan_suggest.py` | 262 | 从 JSON 生成 chapter_plan_draft.md（三圈分配） | ✗ |
| `bid_plan_synthesize.py` | 714 | 另一种 chapter_plan 合成（含 LLM 子项拆分） | ✗ |
| `bid_rules_synthesize.py` | 1274 | 从 JSON 生成 disqualification_rules.md（8 个 YAML 块） | ✗ |

**结果**：每次跑新项目，LLM 要从头手工写出 3 份数百行的结构化文档，耗时且易错。

### 建议

1. **合并 bid_plan_suggest 和 bid_plan_synthesize**：前者是纯规则引擎（262 行），后者调用 LLM 拆子项（714 行）。建议二合一为 `bid_plan_synthesize.py`：先用规则引擎生成草稿，再可选调 LLM 细化子项。

2. **将 bid_brain_translate + bid_difficulty_synthesize + bid_plan_synthesize + bid_rules_synthesize 串联**进 `bid_run.py` 的编排步骤，作为 Step 2 的"自动初稿"模式。

3. **主提示词改为**：Step 2 先跑自动化脚本出草稿，LLM 的角色从"从零写"变为"审阅 + 调整草稿"，确认后进入 `bid_plan_verify` 校验。

4. **保留人工 override**：用户可以选择跳过自动化、手工写，或对自动草稿做调整。

### 预期收益

- 单项目 Step 2 耗时从 30-60 分钟降到 5-10 分钟（审阅确认）
- 消除凭印象重写的系统性风险（v2 核心原则被自动化硬保证）
- 减少 chapter_plan 页数估算错误（自动化查 Word 密度表）

---

## 优化二：B3 硬约束检测增强

### 现状

`bid_skeleton_extract.py` 第 485-491 行的 B3 prompt 关键词清单只有 13 个**结果性触发词**：

```
否决投标、废标、无效投标、不予受理、限制参加、视为撤销、
视为不响应、重大偏差、实质性偏差、直接否决、一票否决、按废标处理、视为无效
```

漏掉**约束性触发词**，如「不得超过」「必须包含」「应于…前」「不得少于」等。这些条款字面不含废标关键词，但违反后会触发兜底条款（如"未响应实质性要求 → 智能化系统否决投标"）。

`memory/feedback_bid_b3_implicit_constraint.md` 记录了这一问题，当前 workaround 是要求 LLM 在 Step 2 手工 grep 补充。

### 建议

1. **在 B3 prompt 的关键词清单中加入约束性触发词**：
   - `不得超过` / `不得大于` / `不得低于`
   - `不得少于` / `至少` / `最少`
   - `必须包含` / `必须涵盖` / `应包括` / `应包含`
   - `应于…前` / `应在…前`
   - `不得` / `禁止` / `严禁`（带数值/参数上下文）
   - `否则` / `视为`（作为约束→后果链接词）

2. **新增 B3 类别 `硬约束`**，区别于 `报价/资格/形式/格式/评审/其他`。

3. **后处理校验**：`bid_skeleton_extract.py` 抽取完成后，对主文档全文做约束词命中统计，命中但未入 `disqualification.json` 的条款生成警告清单，提醒 LLM 在 Step 2 手工补充。

### 预期收益

- 消除页数硬顶/字数硬顶/格式硬约束漏抽的系统性漏洞
- 不再依赖 LLM 每次手工 grep（省 2-3 分钟 + 消除遗忘风险）

---

## 优化三：记忆系统机器化

### 现状

19 条记忆文件记录了大量实战教训，但：

- **无自动化检查**：LLM 必须手动判断"当前项目是否触发某条记忆的条件"
- **无合规集成**：记忆反馈的规则（如"合格制必须 1:1""封面字号禁止硬编码"）没有编译成 `bid_compliance.py` 的检查项
- **无决策矩阵**：规模判档 / 页数硬顶 / 合格制处理等决策规则分散在 19 个文件中，没有统一入口

### 建议

1. **将 9 条 feedback 类记忆编译为机器可执行的检查规则**，集成到 `bid_compliance.py` 或新增 `bid_memory_check.py`：

   | 记忆 | 可编译规则 |
   |------|-----------|
   | `feedback_bid_pass_fail_anchor` | chapter_plan 章节数 ≥ mandatory_outline topics 数 |
   | `feedback_bid_no_hardcode_size` | bid_assemble.py 中不允许存在硬编码 Pt(数字) |
   | `feedback_bid_attachment_chapter` | required 图表的目标章节 = "附图及附表" 章节 |
   | `feedback_bid_mixed_method_passfail` | 混合制 → evaluation_method 强制 passfail |
   | `feedback_bid_size_judge_matrix` | 4 维判档矩阵 → 目标页数自动决策 |
   | `feedback_bid_b3_implicit_constraint` | B3 约束词扫描（见优化二） |
   | `feedback_bid_assemble_fixes` | YAML 别名检查 / 行距规则 / 字号源检查 |
   | `feedback_bid_output_layout` | output/ 目录结构一致性检查 |
   | `feedback_bid_yaml_field_alias` | disqualification_rules YAML 字段有效性 |

2. **在 `bid_plan_verify.py` 中增加记忆规则关卡**：对每条 feedback，检测当前项目是否触发条件，触发则校验是否遵守。

3. **建立 feedback→check 的可追溯性**：每条自动化检查标注来源 feedback，方便事后验证。

### 预期收益

- 19 个记忆文件从"LLM 可能读"变为"机器必然执行"
- 防止已修复过的错误在新项目中重复出现
- 降低 onboarding 成本（新 LLM session 不需要读完 19 个文件才能正确工作）

---

## 优化四：bid_plan_verify 铁律文档与代码对齐

### 现状

`commands/bid-gen.md` 声称 3 条铁律：
1. 评分总分一致性
2. 废标条款来源标注
3. 章节锚点覆盖

但 `bid_plan_verify.py` 实际实现 8 条检查：
1. 评分总分一致性
2. 废标条款来源标注
3. 章节字数总和 ∈ 目标 × [0.85, 1.15]
4. 每章子项字数之和 ≈ 章节目标字数（±5%）
5. 估算总页数 ≤ 目标页数 × 1.05
6. 骨架 JSON 存在
7. brain ↔ 骨架对账
8. 来源页码集中度检测

### 建议

更新 `commands/bid-gen.md` 第 320-334 行的铁律描述，与代码实现对齐。不需要改代码（代码是对的），只需要同步文档。

---

## 优化五：bid_input_assemble 融入主线

### 现状

`bid_input_assemble.py` 将全部前置产物（facts / derived / skeleton / special_clauses / template_match）合并为 `project_input.json`，附带：
- `rejection_lines`：扁平的避雷清单
- `drives_index`：每字段驱动哪些章节

但这个文件**没有被主工作流消费**——LLM 在 Step 2 单独读每个 JSON，而不是读这一份合并后的文件。

### 建议

1. 将 `bid_input_assemble.py` 集成进 `bid_run.py` 的编排步骤（它已在 STEPS 列表中，但提示词未引用其产出）
2. 让 `bid_brain_translate.py` 和其他合成脚本以 `project_input.json` 为**唯一输入源**，而不是各自独立加载 JSON
3. `rejection_lines` 注入 Step 3 writer prompt，作为"禁止踩的坑"清单

### 预期收益

- 单一真相源，消除多脚本各自读 JSON 导致的不一致
- rejection_lines 让下游 writer 多一层废标防御

---

## 优化六：章节生成质量反馈闭环

### 现状

`bid_reviewer.py` 审校后，`bid_run.py` 对 major 项自动重写（最多 2 次）。但没有把审校中发现的问题**反馈回 prompt 模板**以改进后续章节。

### 建议

1. 在 `bid_run.py` 中增加：当某个 variant/章节因相同错误被多次重写时，**标记该模型的 prompt 弱点**，写入 `output/writer_stats.json` 的 `model_weakness` 字段（如 "模型 X 容易漏写工期天数"）

2. 后续章节的 prompt 自动注入"上次该模型遗漏了：XXX，请特别注意"

3. 跨项目积累：模型弱点持久化到 `configs/bid_llm_config.json` 的模型条目下，如 `"known_weakness": ["工期数字遗漏", "表格列数偏多"]`

### 预期收益

- 减少重写次数
- 模型弱点可跨项目追踪
- 为将来模型选型提供数据

---

## 优化七：测试覆盖提升

### 现状

`tests/` 目录仅 7 个文件：
- `conftest.py`
- `test_bid_run_gates.py`（编排层 checkpoint + gate 扫描）
- `test_bid_derive.py`（密度查表）
- `test_bid_common_blind.py`（暗标扫描）
- `test_bid_skeleton_extract.py`（骨架 JSON 校验）
- `test_postprocess_docx.py`（DOCX 后处理）
- `test_step2_regression.py`（Step 2 回归）

**缺失测试的脚本**（按重要性排列）：
- `bid_assemble.py`（2531 行 — 格式解析、DOCX 构建）
- `bid_compliance.py`（673 行 — 合规扫描）
- `bid_writer.py`（1460 行 — prompt 构造、模型调用）
- `bid_charts.py`（2613 行 — 图表渲染器）
- `bid_similarity.py`（521 行 — 相似度计算）
- `bid_brain_translate.py`（423 行 — brain 翻译）
- `bid_rules_synthesize.py`（1274 行 — rules 合成）

### 建议

**Phase 1（高优先级）**：核心管线集成测试
- 准备一份最小化的测试招标文件（3 页 PDF + 评分表 + 清单）
- 建 CI 测试：从 Step 1 → Step 6 全流程跑通，验证各步退出码
- mock LLM 返回值，测试 bid_writer prompt 构造逻辑

**Phase 2（中优先级）**：单元测试
- `bid_assemble.py` 的 `_parse_structured_block` 格式解析
- `bid_compliance.py` 的 `must_include_sections` YAML 解析
- `bid_charts.py` 各渲染器 schema 验证

**Phase 3（低优先级）**：边界与回归
- bid_common.py 工具函数（已有暗标扫描测试，扩展其他函数）
- 各退出码路径覆盖

---

## 优化八：页数估计前移

### 现状

`bid_assemble.py` 组装完 DOCX 后通过 `osascript` 调用 Word 读取真实页数（_check_page_count），但此时发现问题已晚——需要从头改 chapter_plan、重写章节、重组装。

### 建议

1. **在 Step 2 chapter_plan 确认后立即做一次轻量估计**：不组装 DOCX，而是用字数密度 × 图表数估算各章页数，与 chapter_plan 的页数估对比。偏差超过 15% 就报警。

2. **在 Step 3 写完第 3-4 章后做一次抽样验证**：把前几章转成临时 DOCX，osascript 测真实密度，用这个密度反推全文档页数。偏差过大就调整后续章节的字数分配。

3. bid_derive.py 的 chars_per_page 表保持更新（当前已有 v5 实测值，做得好）。

### 预期收益

- 把"超页废标"的发现时间从 Step 4/5 提前到 Step 2/3
- 减少重写返工

---

## 优化九：脚本去重与合并

### 现状

存在功能重叠的脚本对：

| 脚本 A | 脚本 B | 重叠领域 |
|--------|--------|---------|
| `bid_plan_suggest.py` (262L) | `bid_plan_synthesize.py` (714L) | chapter_plan 生成 |
| `bid_skeleton_extract.py` (979L) | `bid_facts_extract.py` | 事实抽取 vs 骨架抽取 |
| `bid_brain_translate.py` (423L) | `bid_plan_synthesize.py` (714L) | 都调用 bid_common 生成结构化内容 |

`bid_assemble.py` (2531 行) 和 `bid_charts.py` (2613 行) 过于庞大，可以考虑模块拆分。

### 建议

1. **合并 bid_plan_suggest + bid_plan_synthesize**：前者是纯规则引擎（Phase 4.1），后者加了 LLM 调用。合并为单一 `bid_plan_synthesize.py`，默认规则引擎，`--llm` 参数启用 LLM 子项拆分。

2. **拆分 bid_assemble.py**：
   - `bid_assemble_format.py`：格式解析（_parse_structured_block、YAML→fmt 映射）
   - `bid_assemble_docx.py`：DOCX 构建（pandoc + python-docx）
   - `bid_assemble_post.py`：后处理（暗标扫描、元数据清理、页数检查）
   - `bid_assemble.py`：协调层（薄封装，调用上述模块）

3. **拆分 bid_charts.py**：按图表类型拆为 `charts_schedule.py`、`charts_org.py`、`charts_site.py`、`charts_flow.py`、`charts_resource.py`、`charts_network.py`，保留 `bid_charts.py` 做注册中心。

### 预期收益

- 单文件可维护性提升
- 新加入图表类型不需改动 2600 行的单一文件

---

## 优化十：配置收敛

### 现状

配置分散在多处：
- `configs/bid_llm_config.json`：6 家模型配置
- `configs/bid_vision_config.json`：OCR 触发规则
- `.env` / `~/.env`：API Key
- `disqualification_rules.md` 内嵌 YAML：格式参数（每个项目一份）
- `project_brain.md` 内嵌 YAML：项目事实（每个项目一份）
- `memory/` 19 个 .md：经验规则

### 建议

1. 将 `disqualification_rules.md` 中的格式 YAML 块标准化为 JSON Schema，供 `bid_assemble.py` 和 `bid_compliance.py` 验证
2. 在 `.env.example` 中增加 `BID_GEN_CONFIG_OVERRIDE` 环境变量，允许用户用一个 JSON 文件覆盖所有默认配置
3. 建立一个 `configs/schema/` 目录，存放各 JSON 产物的 JSON Schema 定义，供 CI 和脚本自检

---

## 优先级矩阵

| 优先级 | 优化项 | 工作量 | 影响 | 风险 |
|:---:|---|:---:|:---:|:---:|
| **P0** | 优化一：Step 2 自动化串联 | 中 | 极高 | 低 |
| **P0** | 优化二：B3 硬约束检测 | 低 | 高 | 低 |
| **P1** | 优化三：记忆系统机器化 | 中 | 高 | 中 |
| **P1** | 优化八：页数估计前移 | 中 | 高 | 低 |
| **P1** | 优化五：bid_input_assemble 融入 | 低 | 中 | 低 |
| **P2** | 优化四：文档与代码对齐 | 低 | 低 | 无 |
| **P2** | 优化六：质量反馈闭环 | 中 | 中 | 中 |
| **P2** | 优化七：测试覆盖提升 | 大 | 中 | 低 |
| **P3** | 优化九：脚本去重与合并 | 大 | 中 | 高 |
| **P3** | 优化十：配置收敛 | 中 | 低 | 中 |

---

## 实施建议

**第一期（1-2 天）**：P0 项
- 串联 bid_brain_translate + bid_plan_synthesize + bid_rules_synthesize 进 bid_run 编排
- B3 prompt 增加约束性触发词
- 更新 commands/bid-gen.md 铁律描述

**第二期（3-5 天）**：P1 项
- 记忆反馈机器化（至少覆盖 4-5 条高频反馈）
- 页数估计前移到 Step 2/3
- bid_input_assemble 作为合成脚本的统一输入

**第三期（后续迭代）**：P2/P3 项
- 质量反馈闭环
- 测试覆盖
- 大脚本拆分
- 配置收敛

---

## 附录：调查说明

本次调查包含：
1. **全源码审查**：29 个 Python 脚本（共约 18,000 行）、1 个主提示词（696 行）、19 条记忆文件、3 份配置文件
2. **真实项目对照**：民房类项目作为代表性实例（合格制，70 页硬顶，16 章完整规划）
3. **历史教训追溯**：v2 方法论文档 + 9 条 feedback 记忆 + 4 个历史项目案例
4. **外部搜索**：尝试 Bing 搜索行业最佳实践（搜索服务暂不可用，以领域知识补充）

核心发现：系统设计理念先进（骨架优先、闭环校验、多模型协作），但**自动化程度与已存在的脚本能力不匹配**——已有 5 个脚本可以实现 Step 2 的 80% 自动化，却未被主工作流集成。这一发现贯穿了上述 10 项优化建议。
