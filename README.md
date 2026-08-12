# graph-engineering

`graph-engineering` 是面向工程 Agent 协作的声明式状态图引擎。状态、角色、写入权限、
流转边与循环全部来自工作区 YAML；核心代码不认识“研发 / Reviewer / QA”等业务角色。

## 不变量

- 配置以版本和 SHA-256 内容哈希冻结；变更前必须暂停工作区并递增版本。
- Event Log 是工作区的 append-only 事实源，使用文件锁、完整 JSONL 记录与 `fsync`。
- 服务端消费前再次校验 actor、状态、版本与人工事件因果关系。
- 路由只由冻结配置决定；组织者只能做摘要和可见投递，不能自行选择目标。
- outbox 意图先于外部投递持久化；结果不明确时进入 `needs_reconcile`，不盲目重发。
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

## 工作区生命周期

```bash
graphctl config validate examples/passive-qa-e2e.yaml
graphctl workspace register examples/passive-qa-e2e.yaml
graphctl workspace provision passive-qa-e2e
graphctl workspace resume passive-qa-e2e
graphctl event append passive-qa-e2e \
  --actor organizer --state pending_development --message '实现任务说明'
```

`workspace provision` 只通过 botmux Dashboard API 工作：复用已确认的飞书登录态，创建
全新应用，写 Role Profile，创建群、话题和 session，并原子保存可恢复 checkpoint。
重复执行不会重复创建已完成资源。

需要人工时，任意非组织者 Agent 写 `human_required`；组织者在收到人工回复后写
`human_resolved --causation-id <原事件ID>`，引擎恢复原始写入者。

## 开发验证

```bash
ruff check src tests
pytest
```
