from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from graph_engineering.botmux import BotmuxError, DeliveryUncertain
from graph_engineering.config import WorkspaceConfig
from graph_engineering.engine import StateGraphEngine
from graph_engineering.eventlog import EventLog, EventLogCorrupt
from graph_engineering.models import Delivery, Event, RouteDecision, utc_now
from graph_engineering.storage import Storage


class Dispatcher(Protocol):
    async def dispatch(
        self,
        delivery: Delivery,
        decision: RouteDecision,
        events: Sequence[Event],
        config: WorkspaceConfig,
    ) -> str: ...

    async def recover(
        self,
        delivery: Delivery,
        active_agent: str,
        recent_events: Sequence[Event],
        config: WorkspaceConfig,
    ) -> str: ...


class WorkspaceProcessor:
    def __init__(self, storage: Storage, dispatcher: Dispatcher):
        self.storage = storage
        self.dispatcher = dispatcher

    async def process(self, config: WorkspaceConfig, event_log: EventLog) -> None:
        runtime = await self.storage.get_runtime(config.workspace.id)
        if runtime is None or runtime.status != "running":
            return
        identity = event_log.file_identity()
        if runtime.event_log_identity is not None and runtime.event_log_identity != identity:
            runtime.status = "unhealthy"
            runtime.health = "eventlog_corrupt"
            runtime.last_error = (
                f"event log identity changed from {runtime.event_log_identity} to {identity}"
            )
            await self.storage.save_runtime(runtime)
            return
        try:
            entries = event_log.read_entries_from(runtime.cursor, expected_identity=identity)
        except EventLogCorrupt as exc:
            runtime.status = "unhealthy"
            runtime.health = "eventlog_corrupt"
            runtime.last_error = str(exc)
            await self.storage.save_runtime(runtime)
            return
        if identity is not None and runtime.event_log_identity is None:
            runtime.event_log_identity = identity
            await self.storage.save_runtime(runtime)
        if not entries:
            return

        index = 0
        engine = StateGraphEngine(config)
        all_events, _ = event_log.read_from(0)
        lookup = {event.event_id: event for event in all_events}
        active_node = runtime.active_node
        while index < len(entries):
            state_id = entries[index][0].state_id
            batch: list[tuple[Event, int]] = []
            while index < len(entries) and entries[index][0].state_id == state_id:
                batch.append(entries[index])
                index += 1
            events = [event for event, _ in batch]
            previous_active_node = active_node
            try:
                decision = engine.decide(
                    events,
                    event_lookup=lookup,
                    expected_active_node=active_node,
                )
            except (ValueError, KeyError) as exc:
                runtime.status = "unhealthy"
                runtime.health = "invalid_event"
                runtime.last_error = str(exc)
                await self.storage.save_runtime(runtime)
                return

            delivery_id = self._delivery_id(config, events)
            existing = await self.storage.get_delivery(delivery_id)
            if existing is None:
                delivery = Delivery(
                    delivery_id=delivery_id,
                    workspace_id=config.workspace.id,
                    event_ids=decision.event_ids,
                    target_agent=decision.target_agent,
                    status="pending",
                )
                await self.storage.save_delivery(delivery)
                await self._deliver(delivery, decision, events, config)
            elif existing.status == "pending":
                existing.status = "needs_reconcile"
                existing.detail = (
                    "process restarted after intent persistence with unknown delivery result"
                )
                existing.updated_at = utc_now()
                await self.storage.save_delivery(existing)

            latest_runtime = await self.storage.get_runtime(config.workspace.id)
            if (
                latest_runtime is None
                or latest_runtime.status != "running"
                or latest_runtime.cursor != runtime.cursor
                or latest_runtime.config_version != runtime.config_version
                or latest_runtime.config_hash != runtime.config_hash
            ):
                return
            runtime = latest_runtime
            runtime.cursor = batch[-1][1]
            runtime.active_node = decision.active_node
            active_node = decision.active_node
            if decision.action == "close":
                runtime.suspended_node = previous_active_node
            elif decision.action == "activate":
                runtime.suspended_node = None
            runtime.last_error = None
            runtime.health = "unknown"
            runtime.updated_at = utc_now()
            runtime.status = {
                "activate": "running",
                "complete": "completed",
                "close": "closed",
            }[decision.action]
            await self.storage.save_runtime(runtime)
            if runtime.status != "running":
                return

    async def _deliver(
        self,
        delivery: Delivery,
        decision: RouteDecision,
        events: Sequence[Event],
        config: WorkspaceConfig,
    ) -> None:
        try:
            detail = await self.dispatcher.dispatch(
                delivery.model_copy(deep=True), decision, events, config
            )
            delivery.status = "delivered"
            delivery.message_id = detail
            delivery.reconciliation_source = "organizer_receipt"
            delivery.detail = "organizer returned a confirmed visible message id"
        except DeliveryUncertain as exc:
            delivery.status = "needs_reconcile"
            delivery.detail = str(exc)
        except BotmuxError as exc:
            delivery.status = "failed"
            delivery.detail = str(exc)
        delivery.updated_at = utc_now()
        await self.storage.save_delivery(delivery)

    @staticmethod
    def _delivery_id(config: WorkspaceConfig, events: Sequence[Event]) -> str:
        source = ":".join(
            [
                config.workspace.id,
                str(config.workspace.version),
                *(event.event_id for event in events),
            ]
        )
        return "delivery-" + hashlib.sha256(source.encode()).hexdigest()[:32]
