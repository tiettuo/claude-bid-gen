# Round 9: 全流程自动化瓶颈攻坚

> 研究日期：2026-05-16
> 分析：8 个项目的 bid_run state + optimization_proposal 执行状态

---

## 发现

### 1. 优化一已落实（✅）
bid-gen.md 已引用 4 个 Step 2 自动化脚本各 1 次 — v3 已升级。Step 2 不再是从零手写。

### 2. 合规扫描普遍失败（🔴）
5/8 项目的 compliance step exit_code=1。不是阻断级失败（流程继续），但说明 compliance_scan 不完整或 disqualification_rules 不完整导致扫描项不足。

交叉验证 R6：大多数项目缺失 B3 隐式约束，compliance step 的失败可能与此有关。

### 3. Skeleton extract 被频繁降级（🟡）
多个项目的 skeleton step status=skip (exit=1 downgraded)。如民房类项目：「skeleton: skip, note: exit=1 downgraded」。可能是 mandatory_outline 未找到 → 降级为软警告。

### 4. 铜溪派出所 review gate 失败（🔴）
Ch03/10/11 各重写 2 次仍 major → gate 失败。2 次重写上限后无后续策略 → 输出带 major 的方案继续。需要第 3 方案（如换模型重写）。

### 5. 多步骤被跳过
fingerprint/chart_specs 在大多数项目中 skip —— 可能是因为 Promptr 中标记为「推荐」而非「必须」。

### 6. Extract 性能瓶颈
古露 5 村 extract 耗时 25,866 秒（7.2 小时）—— 极可能 OCR 卡住或重试风暴。

---

## 建议

### P0
1. Compliance failure 后不静默继续 → 生成警告报告并请求用户确认
2. Review gate 2 次失败后 → 换模型重写（第三方案）

### P1
3. Skeleton downgrade 时收集理由并汇总到 brain.md
4. fingerprint/chart_specs 改为默认 running
