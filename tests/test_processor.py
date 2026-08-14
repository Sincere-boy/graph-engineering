import asyncio
from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.botmux import BotmuxError, DeliveryUncertain
from graph_engineering.config import WorkspaceConfig
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Delivery, Event, WorkspaceRuntime
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
    saved_delivery = await storage.get_delivery(delivery.delivery_id)
    assert saved_delivery.status == "delivered"
    assert saved_delivery.message_id == "visible-message-id"
    assert saved_delivery.reconciliation_source == "organizer_receipt"
    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.cursor > 0
    assert runtime.active_node == "checker"


@pytest.mark.asyncio
async def test_pause_during_delivery_is_not_overwritten_by_stale_runtime(tmp_path: Path) -> None:
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
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingDispatcher(RecordingDispatcher):
        async def dispatch(self, delivery, decision, events, config):
            self.deliveries.append((delivery, decision, events, config))
            started.set()
            await release.wait()
            return "visible-message-id"

    dispatcher = BlockingDispatcher()
    processor = WorkspaceProcessor(storage, dispatcher)
    task = asyncio.create_task(processor.process(config, log))
    await started.wait()
    paused = await storage.get_runtime(config.workspace.id)
    paused.status = "paused"
    await storage.save_runtime(paused)
    release.set()
    await task

    saved = await storage.get_runtime(config.workspace.id)
    assert saved.status == "paused"
    assert saved.cursor == 0

    saved.status = "running"
    await storage.save_runtime(saved)
    await processor.process(config, log)
    resumed = await storage.get_runtime(config.workspace.id)
    assert resumed.status == "running"
    assert resumed.cursor > 0
    assert len(dispatcher.deliveries) == 1


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


@pytest.mark.asyncio
async def test_processor_restart_reconciles_persisted_intent_without_redispatch(
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
    event = Event(
        event_id="e1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="maker",
        state_id="inspect",
        message="inspect",
    )
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(event)
    delivery_id = WorkspaceProcessor._delivery_id(config, [event])
    await storage.save_delivery(
        Delivery(
            delivery_id=delivery_id,
            workspace_id=config.workspace.id,
            event_ids=[event.event_id],
            target_agent="checker",
            status="pending",
        )
    )
    dispatcher = RecordingDispatcher()

    await WorkspaceProcessor(storage, dispatcher).process(config, log)

    assert dispatcher.deliveries == []
    assert (await storage.get_delivery(delivery_id)).status == "needs_reconcile"
    assert (await storage.get_runtime(config.workspace.id)).cursor > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (BotmuxError("rejected"), "failed"),
        (DeliveryUncertain("unknown"), "needs_reconcile"),
    ],
)
async def test_processor_distinguishes_known_and_unknown_delivery_failures(
    tmp_path: Path,
    failure: Exception,
    expected_status: str,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / f"{expected_status}.db")
    await storage.initialize()
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
        )
    )
    event = Event(
        event_id=f"event-{expected_status}",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="maker",
        state_id="inspect",
        message="inspect",
    )
    log = EventLog(tmp_path / f"{expected_status}.jsonl")
    log.append(event)

    await WorkspaceProcessor(storage, RecordingDispatcher(failure)).process(config, log)

    delivery_id = WorkspaceProcessor._delivery_id(config, [event])
    assert (await storage.get_delivery(delivery_id)).status == expected_status


@pytest.mark.asyncio
async def test_processor_closes_workspace_from_organizer_control_event(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "closed.db")
    await storage.initialize()
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
            active_node="checker",
        )
    )
    event = Event(
        event_id="close-event",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="organizer",
        state_id="closed",
        message="close this workspace",
    )
    log = EventLog(tmp_path / "closed.jsonl")
    log.append(event)

    await WorkspaceProcessor(storage, RecordingDispatcher()).process(config, log)

    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.status == "closed"
    assert runtime.active_node is None
    assert runtime.suspended_node == "checker"
    assert runtime.cursor > 0


@pytest.mark.asyncio
async def test_processor_dispatches_reopen_to_suspended_active_node(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    storage = SQLiteStorage(tmp_path / "reopened.db")
    await storage.initialize()
    log = EventLog(tmp_path / "reopened.jsonl")
    close_cursor = log.append(
        Event(
            event_id="close-event",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="closed",
            message="pause this workspace",
        )
    )
    end_cursor = log.append(
        Event(
            event_id="reopen-event",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="reopened",
            message="continue the original task",
            causation_id="close-event",
        )
    )
    await storage.save_runtime(
        WorkspaceRuntime(
            workspace_id=config.workspace.id,
            config_version=1,
            config_hash=config.content_hash,
            status="running",
            cursor=close_cursor,
            event_log_identity=log.file_identity(),
            active_node="maker",
        )
    )
    dispatcher = RecordingDispatcher()

    await WorkspaceProcessor(storage, dispatcher).process(config, log)

    runtime = await storage.get_runtime(config.workspace.id)
    assert runtime.status == "running"
    assert runtime.active_node == "maker"
    assert runtime.cursor == end_cursor
    assert len(dispatcher.deliveries) == 1
    assert dispatcher.deliveries[0][1].target_agent == "maker"
