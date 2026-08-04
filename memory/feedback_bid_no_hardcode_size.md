---
name: bid_assemble 禁止硬编码字号字体
description: 投标文件组装脚本所有字号/字体决策必须经 fmt 字典从招标规则解析；禁止凭"行业惯例"塞 Pt(数字)
type: feedback
---
bid_assemble.py 中**任何字号决策都必须从 `fmt` 字典读取**，不允许直接写 `Pt(数字)` 字面量；招标规则未指定的项必须有"回落到 `fmt['font_size_body']`"的明确路径。

**Why：** 2026-05-04 仓储类项目B项目暴露——封面 `_prepend_cover()` 硬编码 26pt 标题 + 18pt 项目名 + 22pt Title 样式，绕过 fmt。招标 10.16 条要求"全文宋体四号"（14pt 统一），硬编码导致封面字号违规 → 格式类废标条款命中。同时 rFonts 缺 `w:hint="eastAsia"`，macOS Word 把中文按 ascii 字段（Times New Roman）渲染，回落到 PingFang/楷体，封面看起来不像宋体。本质问题不是 typo，是设计漏洞：「正文路径规则驱动 / 封面路径硬编码」两套标准并存。

**How to apply：**
1. 写或改 bid_assemble.py 时，搜 `grep -nE "Pt\([0-9]+\)"`，每个硬编码点必须改成 `fmt[...]` 读取
2. 招标可能不规定的字段（如封面字号、Title 样式）在 fmt 解析末尾用 `setdefault` 给默认值 = `font_size_body`，让"招标没规定时全文统一"成为安全默认
3. 所有 `w:rFonts` 必须同时设 `w:hint="eastAsia"`，否则中文字体在 macOS Word/Preview 会渲染异常
4. Step 5 合规闸门必跑 `bid_format_audit.py`（已集成进 bid_compliance.py）——审计 DOCX 实际 run/style 字号字体，违规 region=body/table/h1 时退出码 ≥2 阻断
5. 改完任何字号相关代码后，必须用一个真实项目 build → audit 验证退出码 0 才算修完
