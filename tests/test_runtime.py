from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import WorkspaceConfig
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Event, SessionBinding, WorkspaceProvisioning
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.runtime import RuntimeService
from graph_engineering.storage import SQLiteStorage


class Dispatcher:
    def __init__(self):
        self.count = 0

    async def dispatch(self, *_):
        self.count += 1
        return "om1"


class Dashboard:
    async def dashboard_summary(self):
        return {"service": {"status": "online"}}

    async def sessions(self):
        return [{"sessionId": "s-org", "status": "ready", "workingDir": self.repository}]


@pytest.mark.asyncio
async def test_runtime_processes_registered_workspaces_and_persists_health(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
    await registry.resume(config.workspace.id)
    EventLog(registry.workspace_dir(config.workspace.id) / "eventlog.jsonl").append(
        Event(
            event_id="e1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="maker",
            state_id="inspect",
            message="inspect",
        )
    )
    await storage.save_provisioning(
        WorkspaceProvisioning(
            workspace_id=config.workspace.id,
            role_profile_id="p1",
            chat_id="oc1",
            bindings=[
                SessionBinding(
                    agent_id="organizer",
                    lark_app_id="cli-org",
                    chat_id="oc1",
                    root_message_id="om-org",
                    session_id="s-org",
                )
            ],
        )
    )
    dispatcher = Dispatcher()
    dashboard = Dashboard()
    dashboard.repository = str(config.workspace.repository)
    service = RuntimeService(registry, storage, dispatcher, dashboard)

    await service.process_once()
    await service.health_once()

    runtime = await storage.get_runtime(config.workspace.id)
    assert dispatcher.count == 1
    assert runtime.active_node == "checker"
    assert runtime.health == "healthy"
