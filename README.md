# Graph Engineering

Graph Engineering 是一个在本地运行的多 Agent 协作引擎。你用 YAML 定义参与者、状态和流转规则，
引擎负责校验每次状态变化、保存完整事件记录，并通过 Botmux 和飞书把任务交给下一位 Agent。

它适合需要明确流程的协作任务，例如“开发 → 评审 → 测试 → 完成”。流程可以循环、等待人工处理，
也可以在关闭后从原位置继续。路由始终来自已经冻结的配置，组织者不能临时决定把任务交给谁。

## 第一次使用：先 clone，再让 Codex 配环境

先把仓库 clone 到本地：

```bash
git clone https://github.com/Sincere-boy/graph-engineering.git
cd graph-engineering
```

然后在这个目录里打开 Codex，把下面这句话发给它：

```text
请读取 skills/setup-graph-engineering/SKILL.md，按其中流程配置并验收 Graph Engineering 环境。
```

[`setup-graph-engineering`](skills/setup-graph-engineering/SKILL.md) 会检查并配置：

- 仓库内的日常 [`graph-engineering`](skills/graph-engineering/SKILL.md) skill
- Node.js 22+ 和 Botmux `3.13.0`
- Python 3.12 Conda 环境和 `graphctl`
- Docker Compose MongoDB
- `graph-engineering.service` 用户级 systemd 服务
- 本地控制台、`/livez` 和 `/readyz` 健康检查

Botmux 首次配置和飞书网页授权需要你本人完成，skill 会在这里停下来等你。环境配置 skill 只保留在
仓库里，不会安装到全局 Codex 目录；日常使用的 `graph-engineering` skill 会链接到
`~/.codex/skills/graph-engineering`，仓库更新后会自动使用最新版。

配置完成后，可以手动再跑一次验收：

```bash
skills/setup-graph-engineering/scripts/doctor.sh "$(git rev-parse --show-toplevel)"
```

输出 `READY graph-engineering environment` 才表示环境已经可用。目前自动配置流程支持 Linux
x86_64 或 aarch64，并要求用户级 systemd、Docker Engine 和 Compose v2 可用。

## 创建一个工作区

环境配置好以后，可以直接告诉 Codex 你需要什么流程，例如：

```text
请用 graph-engineering 创建一个开发、评审、测试依次执行的工作区；评审或测试不通过时回到开发。
```

`graph-engineering` skill 会先读取目标仓库，把 Agent、状态、返工路径、人工等待和结束条件画成
Mermaid 图。你确认图以后，它才会创建隔离 worktree、生成配置、登记工作区并初始化 Botmux 和
飞书资源。创建到 `provision` 成功为止，不会自行启动任务。

## 启动任务和推动状态

工作区初始化完成后，单独确认要启动任务，再执行：

```bash
graphctl workspace resume <workspace_id>
graphctl event append <workspace_id> \
  --actor organizer \
  --state <organizer_writable_state> \
  --message '任务说明'
```

之后每位 Agent 只写自己有权写入的状态事件。引擎会再次校验写入者、当前节点和配置版本，再按
配置选择下一位 Agent。状态事件先写入 Event Log，外部消息投递随后执行；即使进程中断，也能从
已经记录的位置继续。

需要人工决定时，当前 Agent 写入 `human_required`。后端会直接在组织者所在的飞书群发出通知，
并保存真实的 Feishu `message_id`。收到人工回复后，组织者写入 `human_resolved`，通过
`--causation-id` 引用原来的 `human_required` 事件，任务会回到提出问题的 Agent。

## 关闭、恢复和开始下一轮

临时停止一个正在运行的工作区：

```bash
graphctl workspace close <workspace_id>
```

关闭操作会追加一条可审计的 `closed` 事件，并保留关闭前的活动节点、配置、Event Log、投递记录和
飞书资源。关闭期间不会继续扫描事件、检查 Session 或触发卡死恢复。

收到用户明确的继续指令后，用 `reopen` 回到关闭前的节点：

```bash
graphctl workspace reopen <workspace_id> --message '继续原任务'
```

`closed` 工作区不能用裸 `resume` 恢复。`reopen` 会引用原来的关闭事件；如果进程恰好在事件写入后
退出，重试会复用已有事件，不会多写一条。

工作区到达 `completed` 后，可以保留原 Event Log 并开始下一轮。新入口必须是配置中允许组织者
写入、且 action 为 `activate` 的状态：

```bash
graphctl workspace resume <workspace_id> \
  --state <organizer_writable_state> \
  --message '下一轮任务说明'
```

## 查看运行情况

后端默认监听 `127.0.0.1:8765`。打开
[http://127.0.0.1:8765/](http://127.0.0.1:8765/) 可以看到：

- 所有工作区的运行状态和健康状态
- 当前活动节点和由冻结配置生成的状态图
- 每个 Agent 登记的固定 Session，以及额外出现的 Session
- Event Log 中的时间、写入者、状态、目标节点和消息

页面每秒拉取一次数据。服务还提供 `/livez`、`/readyz` 和只读的
`/api/v1/workspaces...` 查询接口。

常用命令：

```bash
graphctl workspace status <workspace_id>
graphctl workspace diagram <workspace_id>
graphctl delivery list <workspace_id>
```

投递停在 `needs_reconcile` 时，先去飞书确认消息是否已经出现，再记录已有消息作为证据。这个命令
只修改投递结果，不会重新发送：

```bash
graphctl delivery reconcile <delivery_id> --message-id <om_xxx>
```

## 数据和故障恢复

MongoDB 是默认运行时存储。只有明确设置 `GE_STORAGE_BACKEND=sqlite` 才会使用 SQLite。
配置快照、Event Log 和 checkpoint 位于：

```text
~/.graph_engineering/workspaces/<workspace_id>/
```

业务仓库不保存引擎运行态。配置会按版本和 SHA-256 内容哈希冻结，Event Log 以 append-only JSONL
保存并在写入时执行文件锁和 `fsync`。外部投递意图会先持久化；结果不明确时进入
`needs_reconcile`，系统不会盲目重发。

健康检查会报告固定 Session 缺失，以及同一工作区机器人产生的未登记活跃 Session。卡死 watchdog
默认在 300 秒没有新事件后尝试唤醒当前 Agent，每次间隔 300 秒，最多 3 次。下面这些环境变量可以
调整阈值：

```text
GE_STALL_GRACE_SECONDS
GE_RECOVERY_COOLDOWN_SECONDS
GE_RECOVERY_MAX_ATTEMPTS
```

服务异常时先看：

```bash
systemctl --user status graph-engineering.service
journalctl --user -u graph-engineering.service
docker compose -f compose.yaml ps
```

## 手动安装和开发

日常使用建议走仓库内的环境配置 skill。如果只开发 Python 代码，可以手动准备环境：

```bash
conda create -n graph-engineering -c conda-forge python=3.12
conda run -n graph-engineering pip install -e '.[dev]'
docker compose -f compose.yaml up -d --wait mongodb
graphctl service run --control-dir ~/.graph_engineering
```

在独立 worktree 中开发时，如果 editable 安装仍指向另一个 clone，可以显式使用当前源码：

```bash
PYTHONPATH=src graphctl service run --control-dir ~/.graph_engineering
```

提交前运行：

```bash
ruff check src tests
pytest
```
