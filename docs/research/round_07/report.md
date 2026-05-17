# Round 7: 图表渲染质量分析

> 研究日期：2026-05-16

---

## 发现

### 1. JSON 驱动图表覆盖率
- V02 (DeepSeek): 18 JSON blocks, 2 invalid → 16 有效图 (89% 有效率)
- V03 (Qwen): 15 JSON blocks, 0 invalid → 15 有效图 (100% 有效率)
- V01 charts/ 目录 10 PNG 文件 (580KB)

### 2. 图表类型分布
schedule (横道图) | hierarchy_flow | site_layout | org_chart | resource_curve | aoa_network | process_flow — 7种类型均有覆盖

### 3. 无效 JSON 率
V02 有 2/18 (11%) 的 JSON 无效 — 可能因 LLM 在生成 JSON 时截断或格式错误。

### 4. 缺失图表的章节
V02 中 Ch01/Ch03/Ch09 无 JSON 块 — 但这些章节目标有 1 张图表。图表计划与实际产出有脱节。

### 5. 图表质量 (PNG)
- 单图 21-92KB，尺寸偏小（matplotlib 默认 100dpi）
- 所有图为 PNG 位图格式，非矢量。Word 放大后会模糊

### 建议
- 渲染 DPI 提升到 200+
- JSON 校验：bid_reviewer 检查无效 JSON
- 图表缺失检测：对比 chart_specs vs 实际产出
