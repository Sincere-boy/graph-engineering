import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import WorkspaceConfig
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Event, SessionBinding, WorkspaceProvisioning, utc_now
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.runtime import RuntimeService
from graph_engineering.storage import SQLiteStorage


class Dispatcher:
    def __init__(self):
        self.count = 0
        self.recoveries = []

    async def dispatch(self, *_):
        self.count += 1
        return "om1"

    async def recover(self, delivery, active_agent, recent_events, config):
        self.recoveries.append((delivery, active_agent, recent_events, config))
        return "om-recovery"


class Dashboard:
    async def dashboard_summary(self):
        return {"service": {"status": "online"}}

    async def sessions(self):
        return [{"sessionId": "s-org", "status": "ready", "workingDir": self.repository}]


class ForbiddenDashboard:
    def __init__(self):
        self.calls = 0

    async def dashboard_summary(self):
        self.calls += 1
        raise AssertionError("closed workspace must not inspect botmux")

    async def sessions(self):
        self.calls += 1
        raise AssertionError("closed workspace must not inspect botmux")


class CountingSQLiteStorage(SQLiteStorage):
    def __init__(self, path: Path):
        super().__init__(path)
        self.runtime_writes = 0

    async def save_runtime(self, runtime):
        self.runtime_writes += 1
        await super().save_runtime(runtime)


class ConcurrentDispatcher:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def dispatch(self, *_):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return "om-visible"


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


@pytest.mark.asyncio
async def test_runtime_skips_processing_health_and_recovery_for_closed_workspace(
    tmp_path: Path,
) -> None:
    storage = CountingSQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
    await registry.close(config.workspace.id)
    writes_after_close = storage.runtime_writes
    dashboard = ForbiddenDashboard()
    dispatcher = Dispatcher()
    service = RuntimeService(registry, storage, dispatcher, dashboard, stall_grace_seconds=0)

    await service.process_once()
    await service.health_once()

    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.status == "closed"
    assert storage.runtime_writes == writes_after_close
    assert dashboard.calls == 0
    assert dispatcher.count == 0
    assert dispatcher.recoveries == []


@pytest.mark.asyncio
async def test_runtime_persists_health_alert_only_when_snapshot_changes(tmp_path: Path) -> None:
    storage = CountingSQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
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

    class NeedsAttentionDashboard(Dashboard):
        async def sessions(self):
            return [
                {
                    "sessionId": "s-org",
                    "status": "ready",
                    "workingDir": self.repository,
                    "tuiPromptActive": True,
                }
            ]

    dashboard = NeedsAttentionDashboard()
    dashboard.repository = str(config.workspace.repository)
    service = RuntimeService(registry, storage, Dispatcher(), dashboard)

    await service.health_once()
    writes_after_change = storage.runtime_writes
    await service.health_once()

    assert storage.runtime_writes == writes_after_change


@pytest.mark.asyncio
async def test_runtime_processes_workspaces_concurrently_with_independent_locks(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    configs = []
    for suffix in ["one", "two"]:
        raw = valid_config(tmp_path)
        raw["workspace"]["id"] = f"workspace-{suffix}"
        config = WorkspaceConfig.model_validate(raw)
        configs.append(config)
        await registry.register(config)
        await registry.resume(config.workspace.id)
        registry.event_log(config.workspace.id).append(
            Event(
                event_id=f"event-{suffix}",
                workspace_id=config.workspace.id,
                config_version=1,
                actor_id="maker",
                state_id="inspect",
                message="inspect",
            )
        )
    dispatcher = ConcurrentDispatcher()
    dashboard = Dashboard()
    dashboard.repository = str(tmp_path)
    service = RuntimeService(registry, storage, dispatcher, dashboard)

    await service.process_once()

    assert dispatcher.max_active == 2
    for config in configs:
        assert (await storage.get_runtime(config.workspace.id)).cursor > 0


@pytest.mark.asyncio
async def test_runtime_recovers_idle_active_agent_with_last_five_events_once(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    runtime = await registry.register(config)
    runtime.status = "running"
    runtime.active_node = "maker"
    await storage.save_runtime(runtime)
    event_log = registry.event_log(config.workspace.id)
    for index in range(7):
        event_log.append(
            Event(
                event_id=f"event-{index}",
                workspace_id=config.workspace.id,
                config_version=1,
                actor_id="organizer" if index == 0 else "maker",
                state_id="begin" if index == 0 else "inspect",
                message=f"event {index}",
            )
        )
    runtime = await storage.get_runtime(config.workspace.id)
    runtime.cursor = event_log.path.stat().st_size
    runtime.event_log_identity = event_log.file_identity()
    await storage.save_runtime(runtime)
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
                ),
                SessionBinding(
                    agent_id="maker",
                    lark_app_id="cli-maker",
                    chat_id="oc1",
                    root_message_id="om-maker",
                    session_id="s-maker",
                ),
            ],
        )
    )

    class IdleDashboard(Dashboard):
        async def sessions(self):
            return [
                {"sessionId": "s-org", "status": "idle", "workingDir": self.repository},
                {"sessionId": "s-maker", "status": "idle", "workingDir": self.repository},
            ]

    dispatcher = Dispatcher()
    dashboard = IdleDashboard()
    dashboard.repository = str(config.workspace.repository)
    service = RuntimeService(
        registry,
        storage,
        dispatcher,
        dashboard,
        stall_grace_seconds=0,
        recovery_cooldown_seconds=3600,
    )

    await service.health_once()
    await service.health_once()

    assert len(dispatcher.recoveries) == 1
    delivery, active_agent, recent_events, _ = dispatcher.recoveries[0]
    assert active_agent == "maker"
    assert delivery.kind == "recovery"
    assert delivery.source_cursor == runtime.cursor
    assert delivery.attempt == 1
    assert [event.event_id for event in recent_events] == [
        "event-2",
        "event-3",
        "event-4",
        "event-5",
        "event-6",
    ]
    assert (await storage.get_delivery(delivery.delivery_id)).status == "delivered"
    saved_runtime = await storage.get_runtime(config.workspace.id)
    assert saved_runtime.health == "needs_attention"
    assert "recovery" in saved_runtime.last_error


@pytest.mark.asyncio
async def test_runtime_does_not_recover_working_agent_or_when_event_is_pending(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    runtime = await registry.register(config)
    runtime.status = "running"
    runtime.active_node = "maker"
    await storage.save_runtime(runtime)
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
                ),
                SessionBinding(
                    agent_id="maker",
                    lark_app_id="cli-maker",
                    chat_id="oc1",
                    root_message_id="om-maker",
                    session_id="s-maker",
                ),
            ],
        )
    )

    class MutableDashboard(Dashboard):
        status = "working"

        async def sessions(self):
            return [
                {"sessionId": "s-org", "status": "idle", "workingDir": self.repository},
                {"sessionId": "s-maker", "status": self.status, "workingDir": self.repository},
            ]

    dispatcher = Dispatcher()
    dashboard = MutableDashboard()
    dashboard.repository = str(config.workspace.repository)
    service = RuntimeService(
        registry, storage, dispatcher, dashboard, stall_grace_seconds=0
    )

    await service.health_once()
    assert dispatcher.recoveries == []

    dashboard.status = "idle"
    registry.event_log(config.workspace.id).append(
        Event(
            event_id="pending",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="maker",
            state_id="inspect",
            message="agent already made progress",
        )
    )
    await service.health_once()

    assert dispatcher.recoveries == []


@pytest.mark.asyncio
async def test_runtime_stall_grace_uses_last_consumed_event_time(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "control/state.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    runtime = await registry.register(config)
    runtime.status = "running"
    runtime.active_node = "maker"
    runtime.updated_at = utc_now() - timedelta(hours=1)
    event_log = registry.event_log(config.workspace.id)
    cursor = event_log.append(
        Event(
            event_id="recent-progress",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="begin",
            message="recently activated maker",
        )
    )
    runtime.cursor = cursor
    runtime.event_log_identity = event_log.file_identity()
    await storage.save_runtime(runtime)
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
                ),
                SessionBinding(
                    agent_id="maker",
                    lark_app_id="cli-maker",
                    chat_id="oc1",
                    root_message_id="om-maker",
                    session_id="s-maker",
                ),
            ],
        )
    )

    class IdleDashboard(Dashboard):
        async def sessions(self):
            return [
                {"sessionId": "s-org", "status": "idle", "workingDir": self.repository},
                {"sessionId": "s-maker", "status": "idle", "workingDir": self.repository},
            ]

    dispatcher = Dispatcher()
    dashboard = IdleDashboard()
    dashboard.repository = str(config.workspace.repository)

    await RuntimeService(
        registry,
        storage,
        dispatcher,
        dashboard,
        stall_grace_seconds=300,
    ).health_once()

    assert dispatcher.recoveries == []
