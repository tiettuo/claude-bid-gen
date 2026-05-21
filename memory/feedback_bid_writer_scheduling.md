---
name: bid_writer 多模型调度串行化(待优化)
description: bid_writer.py 任务列表按变体分组,worker 全抓首个变体+单模型信号量阻塞,导致多模型串行而非并行,慢约3倍
type: feedback
---
**问题(2026-05-20 养殖类项目三变体项目复现):** `bid_writer.py` 批量模式跑多变体时,实际只有一个模型在工作,另外两个模型空闲,三家变体几乎串行,总耗时约为理想并行的 3 倍。

**根因:**
1. 任务列表按变体分组构建(scripts/bid_writer.py ~line 1586):`for v in target_variants: for ch in v_chapters: tasks.append(...)` → tasks 顺序是 `[V01×8, V02×8, V03×8]`,未按变体交错。
2. `ThreadPoolExecutor(max_workers=4)` 一次性 submit 全部 24 个任务,4 个 worker 抢到的前 4 个全是 V01 的章节(同一模型 glm)。
3. `--per-model-limit` 默认 2,用 `threading.Semaphore(2)` 实现。glm 信号量只放行 2 个,另外 2 个 worker **阻塞在信号量上、却占着 worker 槽位**,不会去取 V02/V03 的任务。
4. 结果:同一时刻只有 2 个 glm 章节在写,deepseek/kimi 全程空闲,直到 V01 全部 8 章写完才轮到 V02。

**当前临时绕过:** 拆成 N 个独立进程,每进程 `--variant VXX` 各写一家,后台并行启动 → 三模型真正同时开工。

**根因修复方向(择一):**
- A. 任务列表按变体轮转交错:`[V01-ch1, V02-ch1, V03-ch1, V01-ch2, ...]`,让前 N 个 worker 自然分到不同模型。
- B. 信号量获取失败不占 worker 槽位:用 model→子队列调度,worker 取任务前先判断该模型信号量是否可用,不可用则跳到下一个可用模型的任务。
- C. 默认把 `workers` 设为 `per_model_limit × 活跃模型数`,并配合方案 A 的交错排序。

**How to apply:** 改 bid_writer.py 调度前优先做方案 A(改动最小,只调 tasks 构建顺序)。在此之前,多变体批量写作一律拆 `--variant` 多进程并行,不要直接 `bid_writer.py <dir>` 跑全量。
