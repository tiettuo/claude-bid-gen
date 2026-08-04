---
name: bid-gen output 目录布局标准
description: bid-gen 流水线脚本统一使用 output/ 子目录分类，避免根目录混乱
type: feedback
---
bid-gen 项目的 `output/` 目录采用以下分层布局，bid_assemble / bid_compliance / bid_format_audit 已按此约定写入：

```
output/
├── *_施工组织设计.docx          # 最终交付（根目录单文件，bid_assemble 写）
├── 01_*.md ... NN_*.md         # 章节源 md（bid_writer 写或 Claude 写）
├── charts/                     # 图表 PNG（bid_charts 写）
├── charts_data/                # 图表 JSON 数据（bid_charts 写）
├── reports/                    # 合规/审计报告
│   ├── compliance_scan.{json,md}   # bid_compliance 写
│   └── format_audit.{json,md}      # bid_format_audit 写
├── logs/                       # 所有运行日志
│   ├── run.log                     # bid_assemble + bid_run 写
│   ├── compliance.log              # bid_compliance 写
│   └── bid.log                     # bid_run 全流程根日志
├── archive/                    # 历史归档（手工维护，脚本不写）
│   ├── _archive_v*/                # 早期被否决的章节方案
│   └── *_施工组织设计.docx          # 历史 DOCX
└── （隐藏文件，Finder 默认不显示）
    ├── .assemble_tmp/              # bid_charts.preprocess_chapter_dir 写
    └── .bid_run.state.json         # bid_run orchestrator 写
```

**Why：** 2026-05-04 仓储类项目B项目 output/ 出现 30+ 项混杂（3 个 DOCX 同名混淆 + 报告日志正文同级 + 已废弃的 _archive_v1/）。批量整理时定下"产物按职能分目录、归档/缓存集中、根目录只剩单一交付 DOCX + 章节 md + 4 个分类目录"的约定。

**How to apply：**
1. 改任何 bid_* 脚本时，新增产物按"日志→logs/、报告→reports/、临时→.cache 或 .assemble_tmp、归档→archive/"归类，不要往 output/ 根目录扔
2. 脚本写文件前必须 `(output / "子目录").mkdir(exist_ok=True)`，让结构对未初始化的项目自动成型
3. 老项目迁移时只需 `mkdir reports archive && mv ...`，bid-gen 流水线脚本会自动适配新位置
4. 不要把章节 md 挪到子目录——bid_writer/compliance 默认在 chapter_dir 根扫 md，挪了会破坏流程
5. `archive/` 是手工归档区，**脚本绝不写入也不读取**，只作为给用户做版本对比用的"博物馆"
