# n8n 工作流接入指南

n8n 用来**编排 bid-gen Web 服务**(不直接调 Python 脚本),好处是流程图可视化、节点级日志、可加邮件/Slack 等触发器。

## 架构

```
[n8n trigger]
   ↓ (manual / cron / webhook)
[Read 招标 zip]
   ↓
[POST http://localhost:5000/upload]  ← Flask MVP
   ↓ 302 → 提取 job_id
[Wait 30s]
   ↓
[GET /status/<id>?json=1]
   ↓
[IF finished?]
   ├─ false → 回到 Wait 30s (循环轮询)
   └─ true  → [GET /download/<id>] → [保存到磁盘] → [邮件通知(可选)]
```

**关键设计:** n8n 调 Flask API,**不直接调 bid_run.py**。这一层解耦让 bid-gen 升级(改 plan_synthesize 等)不影响 n8n 工作流。

## 安装 n8n

三选一:

### Mac 桌面版(最简单)
1. https://n8n.io/download 下 `.dmg`
2. 拖到 Applications,双击启动
3. 浏览器自动打开 http://localhost:5678

### Docker(推荐团队用)
```bash
docker volume create n8n_data
docker run -d --name n8n -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  --add-host=host.docker.internal:host-gateway \
  n8nio/n8n
```
开浏览器 http://localhost:5678

注意:Docker 里访问 Flask 要用 `http://host.docker.internal:5000` 而不是 `127.0.0.1:5000`。

### npm
```bash
npm install n8n -g
n8n start  # localhost:5678
```

## 导入 workflow

1. n8n 主界面 → **Workflows** → 右上角 `Import from File`
2. 选 `BID_GEN_HOME/web/n8n_workflow.json`
3. 改 `Read 招标 zip` 节点的 `fileSelector` 为实际路径
4. 改 `邮件通知` 节点(默认 disabled)的 toEmail 为你的邮箱;若用 Docker n8n,所有 URL 改 `host.docker.internal:5000`

## 启动 bid-gen Flask

n8n 跑之前 Flask MVP 必须先启动:
```bash
cd BID_GEN_HOME
source ~/.env
python3 web/app.py &  # 后台跑,localhost:5000
```

## 触发方式扩展

替换第一个 `Manual Trigger` 节点可改变触发方式:

| 触发器 | 场景 |
|---|---|
| Manual Trigger | 手动按按钮(默认) |
| Cron | 每天定时(如 `0 9 * * *` 每天 9 点) |
| Webhook | 外部系统 POST 触发(给业务系统集成) |
| Email Trigger | 监听邮件附件 zip 自动跑 |
| FTP Trigger | 监听 FTP 目录新文件 |
| Slack Trigger | Slack 命令触发 |

## 常见问题

**Q: workflow 跑到 POST /upload 报 ECONNREFUSED**
A: Flask MVP 没启;或 Docker n8n 用了 `127.0.0.1` 而不是 `host.docker.internal`。

**Q: 302 重定向后 location 拿不到**
A: HTTP Request 节点默认 follow redirect。要关掉:节点 → Settings → Follow Redirect = false。

**Q: 一直在 Wait 30s 循环不退出**
A: 看 Flask 那边任务是否真完成。/status?json=1 的 finished 字段必须 true 才退出。

**Q: 想批量跑多个招标(并发)**
A: 在 `Read 招标 zip` 前加 `Split In Batches` 节点。或者用 cron + 队列。

## 后续可加

- **复盘统计节点**:跑完后调 GET /jobs 拿历史,推到 Notion / 飞书表格
- **失败重试**:在 GET /status 后加 IF 节点判 `alive=false && !finished` → 自动重新 POST /upload
- **审批节点**:Step 2 三件套生成后停下等人审批(用 Webhook 节点接审批 URL)
