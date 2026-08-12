from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.eventlog import EventLog
from graph_engineering.models import WorkspaceRuntime
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
            if current.status != "paused":
                raise ConfigError("workspace must be paused before configuration changes")
            if config.workspace.version <= current.config_version:
                raise ConfigError("new configuration version must increase after a paused update")
        if current and current.config_hash == config.content_hash:
            return current

        directory = self.workspace_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / f"config-v{config.workspace.version}.yaml"
        if snapshot.exists() and snapshot.read_text(encoding="utf-8") != config.to_yaml():
            raise ConfigError(f"configuration version {config.workspace.version} is already frozen")
        self._atomic_write(snapshot, config.to_yaml())
        self._atomic_write(directory / "workspace.yaml", config.to_yaml())
        runtime = WorkspaceRuntime(
            workspace_id=workspace_id,
            config_version=config.workspace.version,
            config_hash=config.content_hash,
            status="registered" if current is None else "paused",
            cursor=0,
        )
        await self.storage.save_runtime(runtime)
        return runtime

    async def pause(self, workspace_id: str) -> WorkspaceRuntime:
        return await self._set_status(workspace_id, "paused")

    async def resume(self, workspace_id: str) -> WorkspaceRuntime:
        return await self._set_status(workspace_id, "running")

    async def _set_status(self, workspace_id: str, status: str) -> WorkspaceRuntime:
        await self._initialize()
        runtime = await self.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        if runtime.status == "completed" and status == "running":
            raise ConfigError("completed workspace cannot be resumed")
        runtime.status = status
        runtime.last_error = None
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
        lines.append('    paused(("暂停"))')
        for state in config.states.values():
            destination = {
                "complete": "completed",
                "pause": "paused",
                "activate": self._mermaid_id(state.action.target or ""),
            }[state.action.type]
            label = state.display_name.replace('"', "'")
            for writer in state.allowed_writers:
                lines.append(f'    {self._mermaid_id(writer)} -->|"{label}"| {destination}')
        lines.append('    organizer -->|"待人工"| human')
        lines.append('    human -->|"人工已处理"| organizer')
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
        return (
            f"# 角色：{agent.display_name}\n\n{agent.prompt}\n\n"
            f"工作区：`{config.workspace.id}`\n"
            "完成一个语义步骤后只能写入你有权写的状态：\n"
            f"{state_lines or '- 无普通状态写权限'}\n\n"
            "写入命令：\n"
            "```bash\n"
            f"graphctl event append {config.workspace.id} --actor {agent_id} "
            "--state <state_id> --message '<summary>'\n"
            "```\n"
            "需要人工决定时写 `human_required`；不要自行假设人工答案。"
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
