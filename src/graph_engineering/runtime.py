from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Protocol

import uvicorn

from graph_engineering.api import create_app
from graph_engineering.botmux import BotmuxClient
from graph_engineering.dispatcher import BotmuxDispatcher
from graph_engineering.health import evaluate_workspace_health
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
    ):
        self.registry = registry
        self.storage = storage
        self.dispatcher = dispatcher
        self.dashboard = dashboard
        self.poll_interval = poll_interval
        self.health_interval = health_interval
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
                    runtime.health = "degraded"
                    runtime.last_error = f"botmux health read failed: {exc}"
                    await self.storage.save_runtime(runtime)
            return
        for runtime in await self.storage.list_runtimes():
            provisioning = await self.storage.get_provisioning(runtime.workspace_id)
            if provisioning is None:
                if runtime.status == "running":
                    runtime.health = "degraded"
                    runtime.last_error = "workspace has no botmux provisioning record"
                    await self.storage.save_runtime(runtime)
                continue
            config = self.registry.load_config(runtime.workspace_id)
            result = evaluate_workspace_health(
                provisioning.bindings,
                summary,
                sessions,
                expected_repository=str(config.workspace.repository),
            )
            changed = runtime.health != result.status
            runtime.health = result.status
            runtime.last_error = "; ".join(result.reasons) or None
            if changed or runtime.last_error:
                await self.storage.save_runtime(runtime)

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
    runtime = RuntimeService(registry, storage, dispatcher, botmux)
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
