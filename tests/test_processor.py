from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import WorkspaceConfig
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Event, WorkspaceRuntime
from graph_engineering.processor import WorkspaceProcessor
from graph_engineering.storage import SQLiteStorage


class RecordingDispatcher:
    def __init__(self, fail: Exception | None = None):
        self.deliveries = []
        self.fail = fail

    async def dispatch(self, delivery, decision, events, config):
        self.deliveries.append((delivery, decision, events, config))
        if self.fail:
            raise self.fail
        return "visible-message-id"


@pytest.mark.asyncio
async def test_processor_persists_intent_before_dispatch_and_advances_cursor(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "state.db")
    await storage.initialize()
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
        )
    )
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(
        Event(
            event_id="e1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="maker",
            state_id="inspect",
            message="please inspect",
        )
    )
    dispatcher = RecordingDispatcher()
    processor = WorkspaceProcessor(storage, dispatcher)

    await processor.process(config, log)

    delivery = dispatcher.deliveries[0][0]
    assert delivery.status == "pending"
    assert (await storage.get_delivery(delivery.delivery_id)).status == "delivered"
    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.cursor > 0
    assert runtime.active_node == "checker"


@pytest.mark.asyncio
async def test_processor_does_not_repeat_delivered_batch_after_cursor_rewind(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "state.db")
    await storage.initialize()
    runtime = WorkspaceRuntime(
        workspace_id=config.workspace.id,
        config_version=1,
        config_hash=config.content_hash,
        status="running",
    )
    await storage.save_runtime(runtime)
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(
        Event(
            event_id="e1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="maker",
            state_id="inspect",
            message="inspect",
        )
    )
    dispatcher = RecordingDispatcher()
    processor = WorkspaceProcessor(storage, dispatcher)
    await processor.process(config, log)
    runtime = await storage.get_runtime(config.workspace.id)
    runtime.cursor = 0
    await storage.save_runtime(runtime)

    await processor.process(config, log)

    assert len(dispatcher.deliveries) == 1


@pytest.mark.asyncio
async def test_processor_marks_unauthorized_log_unhealthy_without_skipping(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "state.db")
    await storage.initialize()
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
        )
    )
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(
        Event(
            event_id="bad",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="checker",
            state_id="begin",
            message="not allowed",
        )
    )

    await WorkspaceProcessor(storage, RecordingDispatcher()).process(config, log)

    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.status == "unhealthy"
    assert runtime.cursor == 0
    assert "not allowed" in runtime.last_error


@pytest.mark.asyncio
async def test_processor_fails_closed_when_event_log_is_rotated(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "state.db")
    await storage.initialize()
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
        )
    )
    path = tmp_path / "eventlog.jsonl"
    log = EventLog(path)
    log.append(
        Event(
            event_id="e1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="maker",
            state_id="inspect",
            message="first",
        )
    )
    processor = WorkspaceProcessor(storage, RecordingDispatcher())
    await processor.process(config, log)

    replacement = EventLog(tmp_path / "replacement.jsonl")
    for event_id in ["e2", "e3"]:
        replacement.append(
            Event(
                event_id=event_id,
                workspace_id=config.workspace.id,
                config_version=1,
                actor_id="maker",
                state_id="inspect",
                message="replacement",
            )
        )
    replacement.path.replace(path)

    await processor.process(config, log)

    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.status == "unhealthy"
    assert runtime.health == "eventlog_corrupt"
    assert "identity" in runtime.last_error
