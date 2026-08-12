from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, HTTPException

from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import Storage


def create_app(
    storage: Storage,
    *,
    registry: WorkspaceRegistry | None = None,
    runtime_service: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await storage.initialize()
        task = asyncio.create_task(runtime_service.run()) if runtime_service else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="graph-engineering", version="0.1.0", lifespan=lifespan)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        if not await storage.ping():
            raise HTTPException(status_code=503, detail="storage unavailable")
        return {"status": "ready"}

    @app.get("/api/v1/workspaces")
    async def list_workspaces() -> list[dict]:
        return [runtime.model_dump(mode="json") for runtime in await storage.list_runtimes()]

    @app.get("/api/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict:
        runtime = await storage.get_runtime(workspace_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        return runtime.model_dump(mode="json")

    @app.get("/api/v1/workspaces/{workspace_id}/events")
    async def get_workspace_events(workspace_id: str) -> list[dict]:
        if registry is None:
            raise HTTPException(status_code=503, detail="event registry unavailable")
        try:
            events, _ = registry.event_log(workspace_id).read_from(0)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [event.model_dump(mode="json") for event in events]

    @app.get("/api/v1/workspaces/{workspace_id}/deliveries")
    async def get_workspace_deliveries(workspace_id: str) -> list[dict]:
        return [
            delivery.model_dump(mode="json")
            for delivery in await storage.list_deliveries(workspace_id)
        ]

    return app
