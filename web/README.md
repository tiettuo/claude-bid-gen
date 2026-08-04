# bid-gen Web (Flask MVP)

Flask app,把 bid-gen 命令行流水线包装成浏览器可用的服务：上传跑标 + 项目检视器（dashboard）。

## 启动

```bash
cd bid-gen
source ~/.env  # 加载 DEEPSEEK_API_KEY 等
python3 web/app.py            # http://127.0.0.1:5000
python3 web/app.py --port 8080
```

首次启动需:`pip install flask`(已装则跳过)。

## 路由

| 路由 | 说明 |
|---|---|
| `GET /` | 首页:上传表单 + 任务列表 |
| `POST /upload` | 上传 zip → 启动后台 bid_run.py → 重定向到 status |
| `GET /status/<job_id>` | 任务详情页(进度条 + 实时日志,3 秒轮询) |
| `GET /status/<job_id>?json=1` | JSON 状态(给前端/程序化调用) |
| `GET /download/<job_id>` | 下载 docx (单份)或打包 zip(含 brain/plan/rules + 合规报告) |
| `GET /jobs` | JSON 任务列表 |

### Dashboard（项目检视器）

`web/dashboard.py`（blueprint）+ `web/_readers.py`（只读聚合）+ `web/app.py`（POST control）。

| 路由 | 说明 |
|---|---|
| `GET /dashboard` | 项目选择页；带 `?project_dir=` → 完整检视器单页 |
| `GET /api/dashboard/state` | 18 步进度 / runtime / 失败步 / 重跑建议 |
| `GET /api/dashboard/step2` | Step2 三件套 + 3 个骨架 JSON 是否就绪 |
| `GET /api/dashboard/compliance` | 逐 variant 合规风险（红/黄/绿）+ 违规计数 |
| `GET /api/dashboard/verify` | 组装验证 findings（封面/页数/格式） |
| `GET /api/dashboard/similarity` | 相似度防围标矩阵 + 超阈对 |
| `GET /api/dashboard/review` | 机器审校严重性 |
| `GET /api/dashboard/budget` | Token/费用/耗时 vs 预算上限 |
| `GET /api/dashboard/loop` | 循环重试 cycles + 失败台账 |
| `GET /api/dashboard/gates` | 质量门禁自动修复决策 |
| `GET /api/dashboard/charts` | 图表画廊清单（PNG 经 `/asset` 取） |
| `GET /api/dashboard/file?path=` | 项目内文本预览（路径围栏 + `.env` 拒绝） |
| `GET /api/dashboard/asset?path=` | 项目内图片/JSON 取（围栏同 `/file`） |
| `GET /api/dashboard/inspect` | 校验 project_dir 是否有效目录 |
| `POST /api/dashboard/control` | Start/Stop bid_run（`--resume --from --to --force-continue`） |

检视器可指向磁盘上任意投标目录（不限于上传的 job）；面板由原生 JS 轮询，无构建步骤。合规扫描路径单份/批量不一致，聚合层防御性 glob（`output/reports/`、`output/V*/reports/` 及旧平铺路径）。

## 上传字段

- `zipfile`(必填):招标文件 zip 包,解压后顶层放 .docx/.pdf/.xlsx 等
- `company_name`(可选):投标公司名;空则用"投标单位名称"占位
- `target_pages`(可选):目标页数;空则按合同金额自动判档
- `manual_step2`(可选):走 Claude 手工 Step 2(默认全自动)

## 后台进程模型

- `subprocess.Popen` 启动 bid_run.py,stdout 重定向到 `~/.bid_gen_jobs/<job_id>/run.log`
- bid_run.py 自己写 `<tender_dir>/output/run.log`(更结构化)和 `.bid_run.state.json` checkpoint
- 状态接口读 state.json 推断进度,读 run.log 取最后 3000 字给前端展示
- 进程死活通过 `os.kill(pid, 0)` 探活

## 状态持久化

所有 job 元数据存 `~/.bid_gen_jobs/<12位hex>/`:
- `meta.json`:job 元信息(tender_dir / pid / cmd / started_at)
- `tender/`:解压的招标目录(bid_run.py 操作目录)
- `tender/extracted/`:抽取产物 + brain/plan/rules
- `tender/output/`:章节 md / docx / 合规报告
- `run.log`:subprocess stdout 重定向输出

服务重启后 jobs 列表仍可见;在跑任务的 pid 会变成"已中断"(因子进程在父 Flask 重启时被孤儿化)。

## 已知问题

1. **Flask 内置服务器**:仅供本地/小团队;生产环境应换 gunicorn + nginx
2. **无认证**:任何能访问 5000 端口的人都能上传/下载;团队用应加 Basic Auth 或 OAuth
3. **磁盘累积**:JOB 不自动清理;手工 `rm -rf ~/.bid_gen_jobs/*` 或加 cron
4. **同步上传**:超大 zip(>50MB) 会卡住上传请求;可加分片或 chunked
5. **进度估算**:基于 .state.json 的 step 状态;某些子步骤(如 writer 11 章)无细粒度
