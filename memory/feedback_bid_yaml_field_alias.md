---
name: bid_assemble yaml 字段名陷阱
description: disqualification_rules.md 写 yaml 时短形式字段名（top_cm/line_spacing 不带 _rule）会被沉默回退到默认值，bug 在最终 docx 才暴露
type: feedback
session: 已脱敏
---
bid_assemble.py 解析 disqualification_rules.md yaml 时，未识别的字段名会**沉默回退默认值**，不报错不告警。

**已发生 2 次同类 bug：**
1. `line_spacing: 1.5`（缺 `line_spacing_rule: multiple`）→ 默认 rule="exact" → 1.5pt 固定行高 → 字符堆叠成"鬼影"
2. `top_cm: 2.54`（缺 `margin_` 前缀）→ DEFAULT_FORMAT 默认值 2.6/2.5/2.8/2.4 → 上下/左右不对称

**Why:** bid_assemble 缺 yaml schema 校验，Claude 写 yaml 倾向自然短名，与脚本期望的全名（margin_top_cm/line_spacing_rule 等）不匹配时静默失败。

**How to apply:**
- 写 yaml **必须**用脚本期望的全名：`margin_top_cm` / `margin_bottom_cm` / `margin_left_cm` / `margin_right_cm`、`line_spacing` 必带 `line_spacing_rule`、`font_size_body`（不是 `body_size_pt`）等
- 不确定字段名时优先 grep `bid_assemble.py:DEFAULT_FORMAT` 和 `_ALIASES` 字典
- 已加防御：脚本 `_apply_line_spacing` 检测 rule=exact 但值 ≤ 4 时自动判 multiple；`_ALIASES` 已加入 `top_cm/bottom_cm/left_cm/right_cm` 短形式
- 组装完一定要审计 docx：`unzip -p docx word/document.xml | grep pgMar` 验证页边距、`grep w:line` 验证行距
