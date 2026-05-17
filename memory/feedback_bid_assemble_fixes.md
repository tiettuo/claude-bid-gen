---
name: bid_assemble.py 格式解析修复记录
description: 2026-04-30 修复了YAML别名、行距规则、表格字号、H1字号、标题间距，新增页数自动检查
type: feedback
session: 已脱敏
---
bid_assemble.py 的格式解析存在 YAML 键名与 disqualification_rules.md 不匹配的系统性问题，已修复。

**修复清单：**
1. YAML 别名扩充：`font_body`→`font_name_cn`、`font_body_size`→`font_size_body`、`margin_top/bottom/left/right`→`margin_*_cm`、`font_table_size`→`font_size_table`
2. 行距规则自动推断：值含"倍"字时自动设 `line_spacing_rule=multiple`，避免落回默认 `exact`（固定磅数）
3. 表格字号从 YAML 读取（`font_size_table`），不再硬编码 `body_size - 2`
4. H1 标题字号：招标文件未指定时跟随正文，不落回默认三号16pt
5. 标题段前段后间距收紧（H1: 12/6pt, H2: 6/3pt, H3: 3/0pt）
6. 新增 `_check_page_count()`：组装后自动用 Microsoft Word(macOS osascript `compute statistics`)直读真实页数，与 max_pages 对比。**2026-05-04 起 LibreOffice 已彻底移除**,只用 Word — 因为评委看到的就是 Word 渲染的页数,LibreOffice 偏多 ~20-30%。osascript 调用约 7 秒,无外部依赖(Word.app 在 /Applications/ 即可)

**Why:** 首个实际项目（仓储类项目A仓储中心）暴露了 YAML 键名不匹配导致字体/边距/行距全部落回默认值的问题。同时 chapter_plan 的字数密度推算（900字/页）与实际（14pt+1.5倍行距下约500字/页）严重偏离，导致生成文档严重超页。

**How to apply:** 以后生成 disqualification_rules.md 时，YAML 键名已经通过别名兼容，不需要刻意匹配内部键名。但 chapter_plan 的字数密度推算必须按实际格式参数计算（14pt+1.5倍行距 ≈ 500字/页，12pt+1.5倍行距 ≈ 750字/页），不能用固定的900字/页。
