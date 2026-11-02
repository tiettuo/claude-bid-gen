---
name: 图表渲染两个硬限制（aoa 平行分支 / pandoc 图题孤立）
description: aoa_network 渲染器不懂拓扑，平行工序画在同一水平线上标签重叠到不可读；pandoc 图题在横版分节符处会掉到下一页页首成为孤立行
type: feedback
---
**2026-09-27 某藏北乡镇派出所改扩建项目实测：**

## 1. `aoa_network` 渲染器不认拓扑 → 必须用纯线性链

我按真实双代号网络写了 10 条工序（8 条关键线路 + `3→6 机电预留预埋`、`6→8 周转材料退场` 两条平行非关键工序）。渲染结果 **`bid_charts.render_aoa_network` 把平行工序画在同一条水平线上**：

- `3→4 主体结构工程(40)` 与 `3→6 机电预留预埋及管线敷设(20)` 的标签在节点 3~4 之间**完全重叠**，糊成一团
- `6→7 安装调试工程(25)` 与 `6→8 周转材料退场与场地清理(12)` 同理

**结论：** `render_aoa_network` 只有一维线性布局，**不支持平行分支**。写 附表四 进度网络图时只能用**纯线性关键线路**，把控制性工序（冬期养护穿插）用文字说明"已计入结构阶段"，不要写成平行工序。宁可"网络图退化为一串工序"，也不能交付一张标签重叠不可读的图。

线性 7 工序终态（和数 = 180 = 招标总工期）：施工准备10 → 基础工程30 → 主体结构50 → 屋面防水22 → 装饰装修28 → 安装调试25 → 竣工验收15。

## 2. pandoc 图题在横版分节处变孤立行

`bid_charts` 原先输出 `![图题](charts/x.png)`，pandoc 会把它变成带 caption 的 figure。招标要求"平面布置图、进度图横向编辑"，`bid_assemble._fit_images` 会给 site_layout/aoa_network **单独开横版分节**——图题段落落在分节符**之后**，于是下一页（竖版）**页首**出现一行孤立文字：

```
第 60 页首行：施工总平面布置图
第十六章 其他
```

**已修（`scripts/bid_charts.py` 两处 replacement）：** 改成空 alt `![](charts/x.png)`。所有渲染器都把图题画进 PNG 内部（matplotlib `ax.set_title` / `_t(50, top+1.2, title ...)`、graphviz `g.attr(label=title, labelloc="t")`），pandoc 图题本来就是重复信息。改后第 60 页首行恢复为「第十六章 其他」。`bid_charts_selftest.py` 14/14 PASS、`pytest tests/` 346 passed。

## 3. 顺带：`_fit_images` 后的实际页高占用

实测（A4 纵向版心高 9.69 in / 24.62 cm）：

| 图 | 最终尺寸 | 原图 |
|---|---|---|
| site_layout | 15.91×11.95 cm | 1105×830 |
| org_chart | 15.91×8.96 cm | 842×474 |
| aoa_network（线性 7 工序） | 15.91×2.49 cm | 1431×224 |
| process_flow（分支流 TB） | 12.13×13.00 cm | 883×946 |
| process_flow（链状流 LR） | 15.91×1.03 cm | 1440×93 |

**分支流程图必须把 `variants.json → chart_style.flow_direction` 设成 `LR`。** 默认 `TB`（`bid_assemble._make_chart_style` 的 V01 兜底值是 TB）会把 7 节点带判断回边的流程图渲染成 363×1233 的竖条，`_fit_images` 压到 **3.83 cm 宽**——正文里的字约 1.5 mm，等于交付一张不可读的图。设 LR 后变 1440×93，15.91 cm 宽可读。
