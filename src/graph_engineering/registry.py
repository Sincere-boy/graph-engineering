from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.engine import StateGraphEngine, aggregate_consecutive
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Event, WorkspaceRuntime, utc_now
from graph_engineering.storage import SQLiteStorage, Storage


class WorkspaceRegistry:
    def __init__(self, control_dir: Path, storage: Storage | None = None):
        self.control_dir = control_dir
        self.storage = storage or SQLiteStorage(control_dir / "state.db")
        self._initialized = False

    async def _initialize(self) -> None:
        if not self._initialized:
            self.control_dir.mkdir(parents=True, exist_ok=True)
            await self.storage.initialize()
            self._initialized = True

    def workspace_dir(self, workspace_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", workspace_id):
            raise ConfigError("invalid workspace id")
        return self.control_dir / "workspaces" / workspace_id

    def event_log(self, workspace_id: str) -> EventLog:
        return EventLog(self.workspace_dir(workspace_id) / "eventlog.jsonl")

    async def register(self, config: WorkspaceConfig) -> WorkspaceRuntime:
        await self._initialize()
        workspace_id = config.workspace.id
        current = await self.storage.get_runtime(workspace_id)
        if current and current.config_hash != config.content_hash:
            if current.status != "closed":
                raise ConfigError("workspace must be closed before configuration changes")
            if config.workspace.version <= current.config_version:
                raise ConfigError("new configuration version must increase after a closed update")
        if current and current.config_hash == config.content_hash:
            return current

        directory = self.workspace_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / f"config-v{config.workspace.version}.yaml"
        if snapshot.exists() and snapshot.read_text(encoding="utf-8") != config.to_yaml():
            raise ConfigError(f"configuration version {config.workspace.version} is already frozen")
        event_log = self.event_log(workspace_id)
        cursor = 0
        event_log_identity = None
        if current is not None:
            # A new frozen version starts after every record from the previous
            # version.  Rewinding to zero would reinterpret history with the new graph.
            event_log_identity = event_log.file_identity()
            entries = event_log.read_entries_from(
                0,
                expected_identity=event_log_identity,
            )
            cursor = entries[-1][1] if entries else 0
        self._atomic_write(snapshot, config.to_yaml())
        self._atomic_write(directory / "workspace.yaml", config.to_yaml())
        active_node = None
        if current is not None and (
            current.active_node in config.agents or current.active_node == "human"
        ):
            active_node = current.active_node
        suspended_node = None
        if current is not None and (
            current.suspended_node in config.agents or current.suspended_node == "human"
        ):
            suspended_node = current.suspended_node
        runtime = WorkspaceRuntime(
            workspace_id=workspace_id,
            config_version=config.workspace.version,
            config_hash=config.content_hash,
            status="registered" if current is None else "closed",
            cursor=cursor,
            event_log_identity=event_log_identity,
            active_node=active_node,
            suspended_node=suspended_node,
        )
        await self.storage.save_runtime(runtime)
        return runtime

    async def resume(self, workspace_id: str) -> WorkspaceRuntime:
        return await self._set_status(workspace_id, "running")

    async def close(
        self,
        workspace_id: str,
        message: str = "workspace closed from the control plane",
    ) -> WorkspaceRuntime:
        await self._initialize()
        runtime = await self.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        if runtime.status == "closed":
            return runtime
        config = self.load_config(workspace_id)
        event_log = self.event_log(workspace_id)
        pending = event_log.read_entries_from(
            runtime.cursor,
            expected_identity=runtime.event_log_identity,
        )
        if pending:
            if (
                len(pending) != 1
                or pending[0][0].state_id != "closed"
                or pending[0][0].actor_id != "organizer"
            ):
                raise ConfigError("workspace has pending events and cannot be closed yet")
            _, close_cursor = pending[0]
        else:
            close_event = Event(
                event_id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                config_version=config.workspace.version,
                actor_id="organizer",
                state_id="closed",
                message=message,
            )
            close_cursor = event_log.append(close_event, expected_cursor=runtime.cursor)
        runtime.suspended_node = runtime.active_node
        runtime.active_node = None
        runtime.cursor = close_cursor
        runtime.event_log_identity = event_log.file_identity()
        runtime.status = "closed"
        runtime.last_error = None
        runtime.updated_at = utc_now()
        await self.storage.save_runtime(runtime)
        return runtime

    async def reopen(
        self,
        workspace_id: str,
        message: str,
        *,
        event_id: str | None = None,
    ) -> tuple[WorkspaceRuntime, Event, int]:
        await self._initialize()
        runtime = await self.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        if runtime.status != "closed":
            raise ConfigError("only a closed workspace can be reopened")
        if not message.strip():
            raise ConfigError("reopen requires an auditable message")

        config = self.load_config(workspace_id)
        event_log = self.event_log(workspace_id)
        entries = event_log.read_entries_from(
            0,
            expected_identity=runtime.event_log_identity,
        )
        if not entries:
            raise ConfigError("closed workspace has no terminal close event to reopen")
        persisted_reopen: Event | None = None
        end_cursor: int | None = None
        if entries[-1][0].state_id == "reopened":
            persisted_reopen, end_cursor = entries[-1]
            if persisted_reopen.message != message:
                raise ConfigError("persisted reopen event has a different message")
            if len(entries) < 2 or entries[-2][0].event_id != persisted_reopen.causation_id:
                raise ConfigError("persisted reopen event does not follow its close event")
            close_event, close_cursor = entries[-2]
        elif entries[-1][0].state_id == "closed":
            close_event, close_cursor = entries[-1]
        else:
            raise ConfigError("closed workspace has no terminal close event to reopen")
        if close_event.actor_id != "organizer":
            raise ConfigError("terminal close event was not written by the organizer")

        active_node = runtime.suspended_node
        if active_node is None:
            current_events = [
                event
                for event, _ in entries[:-1]
                if event.config_version == config.workspace.version
            ]
            lookup = {event.event_id: event for event, _ in entries}
            engine = StateGraphEngine(config)
            for batch in aggregate_consecutive(current_events):
                decision = engine.decide(
                    batch,
                    event_lookup=lookup,
                    expected_active_node=active_node,
                )
                active_node = decision.active_node
        if active_node is None:
            raise ConfigError("close event has no suspended active node to restore")

        item = persisted_reopen or Event(
            event_id=event_id or str(uuid.uuid4()),
            workspace_id=workspace_id,
            config_version=config.workspace.version,
            actor_id="organizer",
            state_id="reopened",
            message=message,
            causation_id=close_event.event_id,
        )
        if end_cursor is None:
            end_cursor = event_log.append(item, expected_cursor=close_cursor)
        runtime.status = "running"
        runtime.cursor = close_cursor
        runtime.active_node = active_node
        runtime.health = "unknown"
        runtime.last_error = None
        runtime.updated_at = utc_now()
        await self.storage.save_runtime(runtime)
        return runtime, item, end_cursor

    async def _set_status(self, workspace_id: str, status: str) -> WorkspaceRuntime:
        await self._initialize()
        runtime = await self.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        if runtime.status == "completed":
            raise ConfigError("completed workspace requires an organizer restart event")
        if runtime.status == "closed":
            raise ConfigError("closed workspace requires an audited workspace reopen")
        runtime.status = status
        runtime.last_error = None
        runtime.updated_at = utc_now()
        await self.storage.save_runtime(runtime)
        return runtime

    def load_config(self, workspace_id: str) -> WorkspaceConfig:
        return WorkspaceConfig.from_yaml(self.workspace_dir(workspace_id) / "workspace.yaml")

    def list_workspace_ids(self) -> list[str]:
        root = self.control_dir / "workspaces"
        if not root.exists():
            return []
        return sorted(path.name for path in root.iterdir() if path.is_dir())

    def render_mermaid(self, config: WorkspaceConfig) -> str:
        lines = ["flowchart LR"]
        for agent_id, agent in config.agents.items():
            node = self._mermaid_id(agent_id)
            label = agent.display_name.replace('"', "'")
            lines.append(f'    {node}["{label}"]')
        lines.append('    human{{"人工"}}')
        lines.append('    completed(("完成"))')
        lines.append('    closed(("关闭"))')
        lines.append('    suspended["关闭前活动节点"]')
        for state in config.states.values():
            destination = {
                "complete": "completed",
                "activate": self._mermaid_id(state.action.target or ""),
            }[state.action.type]
            label = state.display_name.replace('"', "'")
            for writer in state.allowed_writers:
                lines.append(f'    {self._mermaid_id(writer)} -->|"{label}"| {destination}')
        lines.append('    organizer -->|"待人工"| human')
        lines.append('    human -->|"人工已处理"| organizer')
        lines.append('    organizer -->|"关闭"| closed')
        lines.append('    closed -->|"重新打开"| suspended')
        return "\n".join(lines) + "\n"

    def render_agent_prompt(self, config: WorkspaceConfig, agent_id: str) -> str:
        agent = config.agents.get(agent_id)
        if agent is None:
            raise ConfigError(f"unknown agent: {agent_id}")
        writable = [
            (state_id, state.display_name)
            for state_id, state in config.states.items()
            if agent_id in state.allowed_writers
        ]
        state_lines = "\n".join(f"- `{state_id}`：{name}" for state_id, name in writable)
        if agent_id == "organizer":
            state_lines += "\n- `closed`：关闭工作区（引擎保留控制事件）"
        skill_lines = "\n".join(f"- `{skill}`" for skill in agent.skills)
        restart_protocol = (
            "\n\n工作区处于 completed 时，你可以通过上述命令写入一个配置允许组织者写入的 "
            "activate 状态来开始新一轮；目标仍由该状态的 action 决定。"
            if agent_id == "organizer"
            else ""
        )
        reopen_protocol = (
            f"\n\n工作区处于 closed 时，只有收到用户明确恢复指令后才能执行 "
            f"`graphctl workspace reopen {config.workspace.id} --message '<恢复说明>'`。"
            "后端会恢复关闭前的活动节点；命令不接受 Agent 参数，你不得自行选择恢复目标。"
            if agent_id == "organizer"
            else ""
        )
        agent_handoff_protocol = (
            "禁止 @组织者，不得直接向组织者发送消息，也不得通过任何 Botmux 命令跨话题回报；"
            "后端会在检测到 Event Log 新记录后自行调用组织者。"
            if agent_id != "organizer"
            else ""
        )
        communication_protocol = (
            "通讯协议：组织者使用群会话，不创建固定话题。收到用户需求时不要直接 @ 任何执行 Agent，"
            "只写入 Event Log 并答复用户；后端会再次调用组织者群 Session，再由你严格按 envelope "
            "使用 `botmux dispatch --into` 投递到目标 Agent 的固定话题。禁止使用 `botmux report`、"
            "`botmux dispatch --title` 或 `new-topic`。"
            if agent_id == "organizer"
            else (
                "通讯协议：初始化后始终在当前固定话题内协作，禁止创建新话题。"
                "禁止使用 `botmux report`、`botmux dispatch --title` 或顶层消息回报；"
            )
        )
        return (
            f"# 角色：{agent.display_name}\n\n{agent.prompt}\n\n"
            f"工作区：`{config.workspace.id}`\n"
            "配置的 Skills（存在且适用时必须使用）：\n"
            f"{skill_lines or '- 无'}\n\n"
            "完成一个语义步骤后只能写入你有权写的状态：\n"
            f"{state_lines or '- 无普通状态写权限'}\n\n"
            "写入命令：\n"
            "```bash\n"
            f"graphctl event append {config.workspace.id} --actor {agent_id} "
            "--state <state_id> --message '<summary>'\n"
            "```\n"
            "需要人工决定时写 `human_required`；不要自行假设人工答案。\n\n"
            f"{communication_protocol}"
            f"{agent_handoff_protocol}"
            "完成、失败与返工只通过上述 `graphctl event append` 写入状态事件。"
            f"{restart_protocol}"
            f"{reopen_protocol}"
        )

    @staticmethod
    def _mermaid_id(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
        return normalized if normalized and not normalized[0].isdigit() else f"n_{normalized}"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
