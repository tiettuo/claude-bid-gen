---
name: 组装/生成步骤必须核对产物,不能凭"成功"下结论
description: bid_assemble/bid_writer 等返回"成功 N 份"不等于功能正确;必须开产物核对肉眼可验特征(封面/字体/行距/页边距/三份是否真不同)
type: feedback
---
**2026-05-20 养殖类项目项目连续踩坑的统一根因。**

跑 `bid_assemble --batch` 看到日志结尾 `批量组装完成 成功:3 失败:0` 就认为组装完成、往下走。但**"成功:N"只代表 N 个 docx 文件生成了,不代表功能正确**。该项目一连三个 bug 全是同一根因——没核对产物:
1. **封面缺失** —— 三份都没封面(rules 双轨文件没同步,blind 误为 true)。
2. **字号错** —— 正文按 12pt 排(应 14pt,authoring_rules.md 没同步)。
3. **format_sets 没生效** —— 三份字体/行距/页边距完全相同(A/B/C 定义块缺失)。

三个 bug 在组装日志里**都有明确警告行**(`[format_set] A 在 disqualification_rules.md 中未定义` 等),但只 grep "保存/完成/失败" 没逐行读日志,全漏了。也没打开 docx 肉眼对比。最后是用户追问才发现。

**How to apply:** 组装/生成类步骤跑完,**强制做两件事**:
1. **逐行扫日志的 WARNING**,不只看末行"成功 N 份"。
2. **开产物核对肉眼可验特征**:封面在不在、正文字体字号对不对、多变体三份是否真的不同(可 unzip docx 读 document.xml 的 eastAsia 字体、styles.xml 的 spacing、sectPr 的 pgMar;页数用 Word `with timeout of 500` 包 compute statistics)。
"脚本 exit 0" / "成功 N 份" 只是必要条件,不是验收标准。功能正确要靠核对产物本身。关联 [[feedback_rules_dual_track]]、[[feedback_bid_multi_template_default]]、[[feedback_root_cause_first]]。
