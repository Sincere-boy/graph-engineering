from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Protocol

import uvicorn

from graph_engineering.api import create_app
from graph_engineering.botmux import BotmuxClient, BotmuxError, DeliveryUncertain
from graph_engineering.config import WorkspaceConfig
from graph_engineering.dispatcher import BotmuxDispatcher
from graph_engineering.health import evaluate_workspace_health, session_is_working
from graph_engineering.models import Delivery, WorkspaceProvisioning, WorkspaceRuntime, utc_now
from graph_engineering.processor import Dispatcher, WorkspaceProcessor
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import Storage, storage_from_environment

logger = logging.getLogger(__name__)


class DashboardReader(Protocol):
    async def dashboard_summary(self) -> dict: ...
    async def sessions(self) -> list[dict]: ...


class RuntimeService:
    def __init__(
        self,
        registry: WorkspaceRegistry,
        storage: Storage,
        dispatcher: Dispatcher,
        dashboard: DashboardReader,
        *,
        poll_interval: float = 10,
        health_interval: float = 60,
        stall_grace_seconds: float = 300,
        recovery_cooldown_seconds: float = 300,
        recovery_max_attempts: int = 3,
    ):
        self.registry = registry
        self.storage = storage
        self.dispatcher = dispatcher
        self.dashboard = dashboard
        self.poll_interval = poll_interval
        self.health_interval = health_interval
        self.stall_grace_seconds = stall_grace_seconds
        self.recovery_cooldown_seconds = recovery_cooldown_seconds
        self.recovery_max_attempts = recovery_max_attempts
        self._locks: dict[str, asyncio.Lock] = {}

    async def process_once(self) -> None:
        await self.registry._initialize()
        tasks = []
        for workspace_id in self.registry.list_workspace_ids():
            runtime = await self.storage.get_runtime(workspace_id)
            if runtime and runtime.status == "running":
                tasks.append(asyncio.create_task(self._process_workspace(workspace_id)))
        if tasks:
            await asyncio.gather(*tasks)

    async def _process_workspace(self, workspace_id: str) -> None:
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            config = self.registry.load_config(workspace_id)
            await WorkspaceProcessor(self.storage, self.dispatcher).process(
                config, self.registry.event_log(workspace_id)
            )

    async def health_once(self) -> None:
        await self.registry._initialize()
        try:
            summary, sessions = await asyncio.gather(
                self.dashboard.dashboard_summary(), self.dashboard.sessions()
            )
        except Exception as exc:
            for runtime in await self.storage.list_runtimes():
                if runtime.status not in {"completed"}:
                    new_error = f"botmux health read failed: {exc}"
                    if runtime.health != "degraded" or runtime.last_error != new_error:
                        runtime.health = "degraded"
                        runtime.last_error = new_error
                        await self.storage.save_runtime(runtime)
            return
        for listed_runtime in await self.storage.list_runtimes():
            lock = self._locks.setdefault(listed_runtime.workspace_id, asyncio.Lock())
            async with lock:
                runtime = await self.storage.get_runtime(listed_runtime.workspace_id)
                if runtime is None:
                    continue
                provisioning = await self.storage.get_provisioning(runtime.workspace_id)
                if provisioning is None:
                    if runtime.status == "running":
                        new_error = "workspace has no botmux provisioning record"
                        if runtime.health != "degraded" or runtime.last_error != new_error:
                            runtime.health = "degraded"
                            runtime.last_error = new_error
                            await self.storage.save_runtime(runtime)
                    continue
                config = self.registry.load_config(runtime.workspace_id)
                result = evaluate_workspace_health(
                    provisioning.bindings,
                    summary,
                    sessions,
                    expected_repository=str(config.workspace.repository),
                )
                recovery_reason = await self._recover_stalled_agent(
                    runtime, provisioning, sessions, config
                )
                reasons = [*result.reasons]
                result_status = result.status
                if recovery_reason:
                    reasons.append(recovery_reason)
                    if result_status == "healthy":
                        result_status = "needs_attention"
                new_error = "; ".join(reasons) or None
                changed = runtime.health != result_status or runtime.last_error != new_error
                runtime.health = result_status
                runtime.last_error = new_error
                if changed:
                    await self.storage.save_runtime(runtime)

    async def _recover_stalled_agent(
        self,
        runtime: WorkspaceRuntime,
        provisioning: WorkspaceProvisioning,
        sessions: list[dict],
        config: WorkspaceConfig,
    ) -> str | None:
        active_agent = runtime.active_node
        if runtime.status != "running" or active_agent in {None, "human", "organizer"}:
            return None
        binding = next(
            (item for item in provisioning.bindings if item.agent_id == active_agent), None
        )
        if binding is None:
            return None
        session = next(
            (item for item in sessions if str(item.get("sessionId")) == binding.session_id), None
        )
        if session is None or session.get("quarantined"):
            return None
        status = str(session.get("status", "")).lower()
        if status in {"isolated", "quarantined", "failed"} or session_is_working(session):
            return None
        stalled_for = (utc_now() - runtime.updated_at).total_seconds()
        if stalled_for < self.stall_grace_seconds:
            return None

        event_log = self.registry.event_log(runtime.workspace_id)
        pending = event_log.read_entries_from(
            runtime.cursor,
            expected_identity=runtime.event_log_identity,
        )
        if pending:
            return None
        all_entries = event_log.read_entries_from(
            0,
            expected_identity=runtime.event_log_identity,
        )
        recent_events = [event for event, _ in all_entries[-5:]]
        recoveries = sorted(
            (
                delivery
                for delivery in await self.storage.list_deliveries(runtime.workspace_id)
                if delivery.kind == "recovery"
                and delivery.source_cursor == runtime.cursor
                and delivery.target_agent == active_agent
            ),
            key=lambda delivery: delivery.created_at,
        )
        if recoveries:
            latest = recoveries[-1]
            if latest.status in {"pending", "needs_reconcile"}:
                return f"recovery delivery {latest.delivery_id} is {latest.status}"
            since_attempt = (utc_now() - latest.updated_at).total_seconds()
            if since_attempt < self.recovery_cooldown_seconds:
                return f"waiting for agent after recovery delivery {latest.delivery_id}"
        attempt = len(recoveries) + 1
        if attempt > self.recovery_max_attempts:
            return (
                f"stalled agent {active_agent} exhausted "
                f"{self.recovery_max_attempts} recovery attempts"
            )
        identity = ":".join(
            [runtime.workspace_id, str(runtime.config_version), str(runtime.cursor), active_agent]
        )
        recovery_id = (
            "recovery-"
            + hashlib.sha256(identity.encode()).hexdigest()[:24]
            + f"-{attempt}"
        )
        delivery = Delivery(
            delivery_id=recovery_id,
            workspace_id=runtime.workspace_id,
            event_ids=[event.event_id for event in recent_events],
            target_agent=active_agent,
            status="pending",
            kind="recovery",
            source_cursor=runtime.cursor,
            attempt=attempt,
        )
        await self.storage.save_delivery(delivery)
        try:
            message_id = await self.dispatcher.recover(
                delivery.model_copy(deep=True), active_agent, recent_events, config
            )
            delivery.status = "delivered"
            delivery.message_id = message_id
            delivery.reconciliation_source = "organizer_receipt"
            delivery.detail = "organizer sent an abnormal recovery message"
        except DeliveryUncertain as exc:
            delivery.status = "needs_reconcile"
            delivery.detail = str(exc)
        except BotmuxError as exc:
            delivery.status = "failed"
            delivery.detail = str(exc)
        delivery.updated_at = utc_now()
        await self.storage.save_delivery(delivery)
        return f"recovery delivery {delivery.delivery_id} is {delivery.status}"

    async def run(self) -> None:
        await asyncio.gather(self._process_loop(), self._health_loop())

    async def _process_loop(self) -> None:
        while True:
            try:
                await self.process_once()
            except Exception:
                logger.exception("workspace processing iteration failed")
            await asyncio.sleep(self.poll_interval)

    async def _health_loop(self) -> None:
        while True:
            try:
                await self.health_once()
            except Exception:
                logger.exception("workspace health iteration failed")
            await asyncio.sleep(self.health_interval)


def _dashboard_connection(control_dir: Path) -> tuple[str, str | None]:
    configured_url = os.getenv("GE_BOTMUX_URL")
    configured_token = os.getenv("GE_BOTMUX_TOKEN")
    botmux_dir = Path.home() / ".botmux"
    if configured_url:
        return configured_url, configured_token
    port_path = botmux_dir / ".dashboard-port"
    token_path = botmux_dir / ".dashboard-token"
    if not port_path.exists():
        raise RuntimeError("botmux dashboard port is unavailable; start botmux first")
    port = port_path.read_text(encoding="utf-8").strip()
    token = configured_token
    if token is None and token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    return f"http://127.0.0.1:{port}", token


async def run_from_environment(control_dir: Path) -> None:
    storage = storage_from_environment(control_dir)
    await storage.initialize()
    registry = WorkspaceRegistry(control_dir, storage)
    base_url, token = _dashboard_connection(control_dir)
    botmux = BotmuxClient(base_url, token=token)
    dispatcher = BotmuxDispatcher(botmux, storage.get_provisioning)
    runtime = RuntimeService(
        registry,
        storage,
        dispatcher,
        botmux,
        stall_grace_seconds=float(os.getenv("GE_STALL_GRACE_SECONDS", "300")),
        recovery_cooldown_seconds=float(os.getenv("GE_RECOVERY_COOLDOWN_SECONDS", "300")),
        recovery_max_attempts=int(os.getenv("GE_RECOVERY_MAX_ATTEMPTS", "3")),
    )
    api = create_app(storage, registry=registry, runtime_service=runtime)
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host=os.getenv("GE_API_HOST", "127.0.0.1"),
            port=int(os.getenv("GE_API_PORT", "8765")),
            log_level=os.getenv("GE_LOG_LEVEL", "info"),
        )
    )
    try:
        await server.serve()
    finally:
        await botmux.close()
