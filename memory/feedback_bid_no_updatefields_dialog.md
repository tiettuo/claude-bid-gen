---
name: Word 打开必弹「域引用其他文件」模态框的根因与处置
description: 产物 DOCX 里绝不能写 w:updateFields=true —— Word 打开时必定弹模态框且无偏好项可关，会卡死 AppleScript 页数实测；目录改用静态生成 + 回填真实页码
type: feedback
---

## 症状

`bid_verify.py` 用 AppleScript 打开产物 docx 量页数时，Word 弹模态框：

> **Microsoft Word**
> 该文档包含的域可能引用了其他文件。是否更新该文档中的这些域？
> ［否(N)］ ［是(Y)］

无人点击时脚本挂到 `osascript` timeout；实测伴随 `-609 连接无效`、
`459:574 execution error`，页数实测静默返回 None（**页数门形同虚设**）。

## 根因

产物 `word/settings.xml` 里的 **`<w:updateFields w:val="true"/>`**。

这是 Microsoft 的**安全提示**，不是用户偏好 —— **Word 偏好里没有任何开关能关掉它**
（已逐一排查 `com.microsoft.Word.plist` / Group Container plist，无对应键；
`set display alerts to none` 也压不住，实测仍然弹）。

## 处置（两侧同时做）

### 1. 产物侧：不写 updateFields（唯一的真修法）

`bid_assemble.py` 的 settings 收尾**只移除、不写入** `w:updateFields`：

```python
stale_uf = settings.find(qn("w:updateFields"))
if stale_uf is not None:
    settings.remove(stale_uf)
```

历史遗留的旧文档若带该标志，也会被主动剔除，避免继续弹窗。

### 2. 目录侧：静态生成 + 组装后回填页码

去掉 `updateFields` 后 Word 也**不会**更新 TOC（实测：保留字段 `w:dirty="true"`
再打开并保存，TOC 仍是占位文本）。所以不能再用「TOC 域 + 让 Word 自动更新」，
改为：

- `bid_assemble._prepend_toc` → `_collect_toc_entries()` 从文档收集 Heading1/2，
  `_build_toc_entry()` 生成静态段落：左标题 + 右对齐制表位（点线）+ **页码占位符**
  `@@TOCPGn@@`。
- 新增 `scripts/bid_toc_fill.py`（STEPS 里排在 `visual_verify` 与 `verify` 之间）：
  Word 导出 PDF → PyMuPDF 定位每个标题所在页 → 回填占位符。
- **占位符与最终页码同在右对齐制表位后，长度变化只改变点线、不增减行 →
  回填不改变分页，无需二次量页。**

### 3. 兜底

`bid_verify.page_count()` 的 AppleScript 里显式 `set display alerts to none`，
让任何残留模态弹窗都无法卡死自动化（但这只是兜底，不是修法）。

## 验收判据

- 产物 `settings.xml` **不含** `updateFields`
- 产物 `document.xml` **不含** `@@TOCPG` 残留、**不含** `TOC \o` 域
- 打开 docx **无任何弹窗**，目录已带真实页码
- `bid_verify` 能在 ~15s 内稳定量出页数

**某西藏施组项目实测（2026-09）：** 修复前 61 页时反复弹窗；修复后目录 102 条
全部定位成功（缺 0 条），Word 实测 65 页 ≤ 硬顶 70 页，打开零弹窗。
注意目录本身占约 4 页（102 条 × 四号字），排页数预算时要把这部分算进去。
