# 写作质量整改（2026-08-19）——54 项目全库复盘结论

## 症状（数据实锤）

- 全库 400+ 章节 P/G 统计：**管理类章节（08质量/10安全/11绿色/14民工）均值 0.6-1.2**，
  数据类（01/05/07）1.4-2.0。最差：某藏北医院 0.10、某林区防火项目 0.27、某县 V01 ch10 0.49。
- 模板残留：某县（物资储备库）组织图出现「钢结构班/路基班」，清单 grep=0。

## 五个根因与对应修复（均已落码）

| # | 根因 | 修复位置 |
|---|---|---|
| 1 | 56%(30/54) 项目 `work_structure: []` 派生失败且**静默放行**；63% 项目无 boq.json | rules_synthesize 告警强化；writer Block1 派生失败时回退 brain 招标范围摘录作锚点；bid_run 项目总览加「写作锚点体检」⚠ |
| 2 | 管理章 48/48 走单模型整章（一次调用写 8 子项×233字）| writer：管理类章节强制转子项级（每子项独立全量 Block1） |
| 3 | 子项字数无地板（82 子项掠 19118 字 → 233字/项）| plan_variants `_normalize_subitems`：450 字地板 + 相邻合并（附表章豁免） |
| 4 | `--angles` 缺省 → 三家 angle 全空 → 反串标只剩弱差异 | variants：从 difficulties.json 难点标题自动派生写作主线 |
| 5 | pg_low 旧阈值(0.2/0.4/0.6)下 0.49 仅 minor，不触发重写 | reviewer：阈值收紧为 0.3/0.5/0.7，pg moderate 升格整体 major → review gate 自动 --force 重写 |

## 同批加固

- Block1 新增：负向约束（清单外专业如钢结构/路基/隧道/桥梁严禁写入）+ 管理章铁律（每条制度后必须跟本项目落地方式）。
- polish：注入 Block1 摘要（900字）治"主笔零上下文→通用衔接语"；引言字数动态化（min(400, target/6)）；小章（<1200字）守恒带上限 1.25→1.6（治"382字章+400字引言必超带→润色永远被弃"）。
- system prompt 增三条写作铁律。
- 模型分池（config writer_role）：kimi/sf_qwen_max → mgmt_preferred，glm → data_preferred；plan_variants 子项分配时角色偏好模型置前。

## 模型池历史实测（写正文参考）

kimi 最佳写手（数据 1.83/管理 1.10/字数 79%）；sf_qwen_max 管理最佳 1.26 但字数 89%；
deepseek 字数最稳 91% 但套话 108/400 章；sf_glm47 质量高但字数 50% 腰斩；
sf_glm/minimax 系 P/G 0.54-0.59 不应写正文。

## 教训

1. **质量机制设计好 ≠ 生效**：交叉子项+主笔润色管线早已存在，但触发条件（chapter_model_plan
   或字数>8000）让小项目 100% 绕过。新增质量机制必须检查"默认路径是否真的会走到它"。
2. **fail-open 链条要体检**：boq.json 缺失 → work_structure 空 → writer 静默零锚点写作 →
   管理章空心化，整条链没有一处报警。上游数据缺失必须在最终产物报告里可见。

## 深挖追加（2026-08-19 第二批，审查循环 R4-R13 修复合计 40+ 项）

关键结构性修复：
- plan_variants 步在 write 前强制 ensure_step2()（chapter_plan 缺失时自动合成，不再静默失败）——某县"变体 H2 差异化从未生效"的根因。
- similarity gate heading 重写前先 bid_plan_variants --variant 刷新该家 H2 计划；assign_strategy 消费 section_order_seed，重种 seed 后切分维度真正换轴。
- authoring_rules.md 新增 ## 10 关键响应点段（生产者 extract_response_keywords：工期日历天/质量目标/废标承诺短语）——此前 writer/_RESPONSE_KEYWORDS 与 reviewer/required_response 两端检查恒空转。
- reviewer 消费 required_response（全文级缺失挂最相关章）；与 writer 同源 bid_common.parse_response_keywords。
- schedule_offset_days 从死数据变活（Block3 进度章注入节点偏移表述）。
- 管理类章节强制子项级；polish 注入 Block1 摘要+动态引言字数+小章守恒带 1.6×；子项失败不落盘占位文本。
- 温度真值源 config（default_temperature/temperature_variants），池内轮转防全同；equip_delta sample 防重复。
- 健壮性：rules_synth 三 JSON 缺失降级；page_hard_cap 联动重算字数；control_price 键名修正；scoring_items 数量失配告警；executor=0 防御；summary 各 json.loads 加保护；--from/--to 步骤名校验；resume 全 ok 短路；log 嵌套目录修正。
- 门禁链：similarity/compliance gate 重写后全量 reviewer 复原汇总；review gate 软性 major（pg/boiler）耗尽重试后降级效行不阻断；--variant 复扫覆盖 summary 的三处同类缺陷全部补齐。

审查方法论：deepseek-v4-flash 每轮 2-6 分钟，R4-R13 共 13 轮迭代收敛；每轮修复前先 grep 原文核实真伪（本轮累计误报率约 10%）。

## 收敛结论（2026-08-20 深夜）

R4-R20 审查循环最终收敛：剩余发现均为 [低] 级设计权衡（关键词兜底注入策略 / 门禁 exit 码语义），
非缺陷。累计修复 120+ 项。过程中三个重要教训：

1. **macOS 系统 Python 的 pyc 缓存在 `~/Library/Caches/com.apple.python/`**——不在 `__pycache__`！
   反复"改了不生效/函数行为诡异"排障 1 小时，根源全是 stale cache。排查命令：
   `find ~/Library/Caches -name "*<模块名>*"`；清理后用 `python3 -B` 复跑。
2. **字符串 splice 重构函数极易切坏**：一次 `t.find('elif')` 删"重复分支"删掉 6 个函数、
   一次 splice 把函数体切成双定义。教训：改函数一律用小锚点精确匹配 + 改后立即
   `py_compile` + 跑相关 pytest，不要批量拼接。
3. **审查循环收益递减曲线**：R4-R8 每轮 3-6 个真问题（门禁假通过/整链 abort 级）；
   R15 后多为 [低] 级理论边角。合理止损点=只剩 [低] 且属设计权衡时。
