# 审校系统重构方案

> 日期：2026-05-18
> 来源：Round 11 调研 → 深化设计
> 状态：方案完成，待实施

---

## 背景

`bid_reviewer.py`（592行）是纯规则引擎，零 LLM 调用。逐章做 8 维检查：标题/字数/套话/PG比/标准/关键词/暗标/H2骨架。

**核心问题**：无跨章视角、无 writer 反馈回路、与 compliance 部分重复。

## 重构三阶段

### Phase 1：批量审校 + 跨章一致性（1天）

**新增函数 `review_cross_chapter()`：**
- 数字一致性（工期天数/面积/金额在各章是否矛盾）
- 人名/地名一致性
- 标准引用一致性
- 施工逻辑合理性

**新增反馈机制：**
- review 发现 H2 缺项 → 写入 `.bid_writer_hints.json`
- writer 重写时自动注入提示

**与 compliance.py 去重：**
- reviewer: 内容质量（P/G、套话、跨章一致、H2骨架）
- compliance: 合规红线（盲标、废标、标准编造、大纲覆盖、格式）

### Phase 2：LLM 深度审校（可选，+1天）

仅在规则引擎检出 severity=major 时触发 1 次 LLM 调用（~$0.02），做：
- 规则引擎漏检的逻辑错误
- 跨章内容自相矛盾
- 逐章逐段修改建议

### Phase 3：自愈机制（远期）

审校 → 自动构造修复命令 → 重写 → 再审 → 闭环

## 改动量

- Phase 1: ~200 行新代码
- Phase 2: ~200 行新代码
- 合计: ~400 行，2天

## 相关文件

- `scripts/bid_reviewer.py` — 需修改
- `scripts/bid_writer.py` — 需读取 `.bid_writer_hints.json`
- `scripts/bid_compliance.py` — 部分检查迁移出去
