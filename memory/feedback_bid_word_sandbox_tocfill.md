---
name: Word 沙盒导致的目录页码/页数实测静默失败
description: bid_toc_fill 导出 PDF 必须落 Word 沙盒容器;AppleScript close 不可用;is missing value 比较不可靠;隐藏文件名 Word 打不开;Word 内存里开着旧副本会导出过期 PDF
type: feedback
---
2026-09-27 某藏北乡镇派出所改扩建项目（60 页、合格制、宋体四号）实测，
`bid_toc_fill` 的目录页码回填与 `bid_verify` 的页数实测**连续静默失败**，
逐层剥出 5 个独立缺陷 + 1 个环境事实。全部已落地到代码，别再回退。

## 环境事实：Microsoft Word 是 App Sandbox 应用

```
codesign -d --entitlements - "/Applications/Microsoft Word.app"
  com.apple.security.app-sandbox
  com.apple.security.files.bookmarks.app-scope
  com.apple.security.files.user-selected.read-write      # powerbox 弹窗
  com.apple.security.temporary-exception.files.absolute-path.read-only
  com.apple.security.temporary-exception.files.home-relative-path.read-only
```

结论：
- **读**：只读例外覆盖全盘 → Word 能打开项目目录里的 docx。
- **写**：只能在自家容器 `~/Library/Containers/com.microsoft.Word/Data/`（或 powerbox 授权位置）。
  往 `/var/folders/.../T/` 或项目目录 `save as ... format PDF`，macOS 弹模态框
  「授予文件访问权限 — 需要额外的权限才能访问下列文件: .../bid_tocfill_XXX」。
  **该弹窗没有 Word 偏好项可关，而且在人工点掉之前会卡死后续所有 AppleScript**：
  `open` 静默失败 → `bid_verify` 页数实测报 -1728、目录页码 112/112 全落「—」。

## 五个缺陷（都已修，含回归测试）

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 目录页码全落「—」 | `tempfile.TemporaryDirectory(prefix="bid_tocfill_")` 写 PDF 触发沙盒弹窗 | PDF 输出路径改到 Word 容器（`_word_writable_dir()`） |
| 2 | `PDF 导出失败: ... 不理解"close"信息` | Word for Mac 16.110 **不暴露 `close` 命令**（`close d saving no` / `tell d to close` 都报 -1708，sdef 里查不到） | close 包 `try ... end try`，best-effort；导出成功即算成功 |
| 3 | `toc_entries_in_order` 只返回 1 条、且是整篇目录粘成一条 | `rfind("<w:p ")` 只认带属性的段落，python-docx 生成的是 `<w:p>` → 回看到最后一个带属性段落 | 预扫 `_P_START_RE = re.compile(r"<w:p(?:\s[^>]*)?>")` + `bisect` |
| 4 | 新增/改名的标题定位不到（113 条缺 3 条） | Word 里已开着**同名旧文档**，`open POSIX file` 是 no-op → 导出的是内存里的过期副本 | 先 `shutil.copy2` 成唯一临时名再喂给 Word（`_word_stage_paths`） |
| 5 | `Word 未能打开文档` 误报 / 轮询首轮就退出 | `if d is missing value` 对**文档对象引用**做比较会走对象自身的相等判定，稳定返回 false | 改用布尔标志 `set found to true` |
| 附 | `_tocstage.docx` 带点号前缀时 `-2700 Word 未能打开文档` | Word 拒绝打开隐藏文件 | 临时 docx 用普通名 `_tocstage_<pid>.docx`，finally 里删 |

另外：临时 docx 与 PDF **必须落不同目录** —— 临时 docx 放项目目录（Word 打不开自家容器 Data/ 根下的 docx，报 -2700），PDF 必须落容器（否则弹窗）。

## 全失败不要落「—」

`fill_one` 现在是：**全部条目都没定位到 = 整体失败 → 保留 `@@TOCPGn@@` 占位符不落盘**。
旧行为会把占位符改写成「—」，于是想重跑本步就必须先整篇 `bid_assemble` 才能拿回占位符。
只有部分定位到时才写「—」。

## 验收（某藏北项目定稿数据）

- `bid_toc_fill`：113/113 条目录页码回填，缺 0 条
- `document.xml` 无 `@@TOCPG` 残留；`settings.xml` 无 `updateFields`（打开零弹窗）
- `bid_verify`：61 页 ≤ 硬顶 70 ✓，封面/宋体/页边距 1440twips 全过
- 回归测试：`tests/test_bid_toc_fill.py`（用旧实现跑同一份 XML 会得到
  `['封面标题 目　录 第一章 工程概况', ...]` 这种累积粘连，测试确实有牙齿）

**关联：** [[feedback_bid_no_updatefields_dialog]] 是同一族问题（Word 打开/导出的模态框会卡死自动化）；
本条补充「不用弹窗也能卡死的沙盒路径问题」。
