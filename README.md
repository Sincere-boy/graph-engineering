# graph-engineering

`graph-engineering` 是面向工程 Agent 协作的声明式状态图引擎。状态、角色、写入权限、
流转边与循环全部来自工作区 YAML；核心代码不认识“研发 / Reviewer / QA”等业务角色。

## 不变量

- 配置以版本和 SHA-256 内容哈希冻结；变更前必须暂停工作区并递增版本。
- Event Log 是工作区的 append-only 事实源，使用文件锁、完整 JSONL 记录与 `fsync`。
- 服务端消费前再次校验 actor、状态、版本与人工事件因果关系。
- 路由只由冻结配置决定；组织者只能做摘要和可见投递，不能自行选择目标。
- 组织者绑定工作区群 Session，不创建固定话题；用户在群内 @组织者产生的会话由该群范围覆盖。
  其他 Agent 以 `(workspace_id, agent_id)` 为稳定身份：初始化时各创建一个原生固定话题和
  session，后续投递只允许复用登记的 `root_message_id`/`session_id`。
- Botmux 群回复使用 `chat-topic`；禁止 `new-topic`、`botmux report`、带 `--title` 的
  dispatch 和顶层回报，避免每次交接 fork 新话题。
- outbox 意图先于外部投递持久化；结果不明确时进入 `needs_reconcile`，不盲目重发。
- 健康检查会把同一工作区机器人产生的未登记活跃 session 标记为
  `needs_attention`，用于发现固定话题协议被破坏。
- 卡死 watchdog 会在工作区仍为 `running`、活跃 Agent 的登记 session 已停止工作、
  Event Log 在宽限期内无推进时调用组织者进入异常恢复模式。组织者收到最近 5 条事件，
  只能在该 Agent 的固定话题催其从中断处继续；恢复投递持久化、幂等、带冷却且最多 3 次。
- MongoDB 是默认存储；SQLite 只能通过 `GE_STORAGE_BACKEND=sqlite` 显式选择。
- 业务仓库不保存引擎运行态；配置快照、Event Log 与 checkpoint 位于
  `~/.graph_engineering/workspaces/<workspace_id>/`。

## 安装与启动

```bash
conda create -n graph-engineering -c conda-forge python=3.12
conda run -n graph-engineering pip install -e '.[dev]'
sudo docker compose up -d mongodb
graphctl service run --control-dir ~/.graph_engineering
```

服务默认只监听 `127.0.0.1:8765`，提供 `/livez`、`/readyz` 和只读的
`/api/v1/workspaces...` 查询接口。

启动服务后可直接打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/) 使用
Workspace Graph Console。页面会列出全部工作区及运行/健康状态；选择工作区后可查看
固定 Session 和额外 Session 的实时状态、由冻结配置生成的状态图，以及包含时间、Actor、
目标节点、状态和消息内容的 Event Log（内部事件 ID 不展示）。图中绿色发光节点代表当前
活跃节点。页面每 1 秒自动拉取数据，也可手动刷新；前端只更新发生变化的区域，未变化的
状态图、Event Log、Session 表格和 Workspace 列表不会重复重绘。

在独立 worktree 中开发时，若 Conda 环境的 editable 安装仍指向原仓库，可显式使用当前
源码启动，避免误用其他 worktree：

```bash
PYTHONPATH=src graphctl service run --control-dir ~/.graph_engineering
```

## 工作区生命周期

```bash
graphctl config validate examples/passive-qa-e2e.yaml
graphctl workspace register examples/passive-qa-e2e.yaml
graphctl workspace provision passive-qa-e2e
# 或复用另一个工作区的应用身份，同时创建独立的新群、固定话题和 session
graphctl workspace provision passive-qa-e2e \
  --reuse-bots-from provisioned-source-workspace
graphctl workspace resume passive-qa-e2e
graphctl event append passive-qa-e2e \
  --actor organizer --state pending_development --message '实现任务说明'
graphctl delivery list passive-qa-e2e
```

不再参与调度的工作区应显式关闭；关闭会保留配置、Event Log、投递记录和飞书资源，
但事件扫描、健康检查与异常恢复都会跳过它。只有显式 `resume` 才会重新进入运行态：

```bash
graphctl workspace close passive-qa-e2e
graphctl workspace resume passive-qa-e2e
```

组织者也可以通过保留控制事件关闭工作区；后端消费该记录后进入 `closed`。其他 Agent
无权写入，工作区配置也不能重定义这个状态：

```bash
graphctl event append passive-qa-e2e \
  --actor organizer --state closed --message '本工作区验收已结束'
```

工作区进入 `completed` 后仍可开始新一轮任务。恢复事件必须由组织者写入一个配置中
`allowed_writers` 包含组织者且 action 为 `activate` 的状态；目标 Agent 继续由配置决定：

```bash
graphctl workspace resume passive-qa-e2e \
  --state pending_development --message '下一轮任务说明'
# 等价方式：直接追加同一个组织者事件
graphctl event append passive-qa-e2e \
  --actor organizer --state pending_development --message '下一轮任务说明'
```

恢复操作会保留上一轮完整 Event Log，追加新的审计事件，并从该事件继续消费；裸
`workspace resume` 仍只用于尚未完成的 registered/paused 工作区。

`workspace provision` 只通过 botmux Dashboard API 工作：复用已确认的飞书登录态，默认
创建全新应用；指定 `--reuse-bots-from` 时按稳定 Agent ID 复用来源工作区的应用身份，但
始终为目标工作区创建独立的新群、组织者群 Session，以及其他 Agent 的固定话题和 session。
命令会写 Role Profile，并原子保存可恢复 checkpoint。
重复执行不会重复创建已完成资源；它会重新协调 Bot 配置、群成员与 Role Profile，
因此协议修复会应用到既有固定话题而不新增话题。

需要人工时，任意非组织者 Agent 写 `human_required`；组织者在收到人工回复后写
`human_resolved --causation-id <原事件ID>`，引擎恢复原始写入者。

投递结果为 `needs_reconcile` 时，先从飞书确认已有可见消息，再显式记录证据；该命令
只更新投递结果，不会重发消息：

```bash
graphctl delivery reconcile <delivery_id> --message-id <om_xxx>
```

watchdog 默认等待 300 秒，每次恢复后冷却 300 秒，最多尝试 3 次。可通过
`GE_STALL_GRACE_SECONDS`、`GE_RECOVERY_COOLDOWN_SECONDS`、
`GE_RECOVERY_MAX_ATTEMPTS` 调整；不确定投递会停在 `needs_reconcile`，不会自动重发。

## 开发验证

```bash
ruff check src tests
pytest
```
