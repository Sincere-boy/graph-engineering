from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import Storage


def create_app(
    storage: Storage,
    *,
    registry: WorkspaceRegistry | None = None,
    runtime_service: Any | None = None,
) -> FastAPI:
    dashboard = getattr(runtime_service, "dashboard", None)

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
    assets_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=assets_dir), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        return FileResponse(assets_dir / "index.html")

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

    @app.get("/api/v1/workspaces/{workspace_id}/graph")
    async def get_workspace_graph(workspace_id: str) -> dict:
        runtime = await storage.get_runtime(workspace_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        if registry is None:
            raise HTTPException(status_code=503, detail="workspace registry unavailable")
        try:
            config = registry.load_config(workspace_id)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        visible_active_node = runtime.active_node
        if visible_active_node is None and runtime.status in {"paused", "completed"}:
            visible_active_node = runtime.status
        nodes = [
            {
                "id": agent_id,
                "label": agent.display_name,
                "kind": "agent",
                "active": visible_active_node == agent_id,
            }
            for agent_id, agent in config.agents.items()
        ]
        nodes.extend(
            [
                {
                    "id": "human",
                    "label": "人工",
                    "kind": "human",
                    "active": visible_active_node == "human",
                },
                {
                    "id": "completed",
                    "label": "完成",
                    "kind": "terminal",
                    "active": visible_active_node == "completed",
                },
                {
                    "id": "paused",
                    "label": "暂停",
                    "kind": "terminal",
                    "active": visible_active_node == "paused",
                },
            ]
        )
        edges = []
        for state_id, state in config.states.items():
            target = state.action.target or {
                "complete": "completed",
                "pause": "paused",
            }[state.action.type]
            edges.extend(
                {
                    "source": writer,
                    "target": target,
                    "state_id": state_id,
                    "label": state.display_name,
                }
                for writer in state.allowed_writers
            )
        edges.extend(
            [
                {
                    "source": "organizer",
                    "target": "human",
                    "state_id": "human_required",
                    "label": "待人工",
                },
                {
                    "source": "human",
                    "target": "organizer",
                    "state_id": "human_resolved",
                    "label": "人工已处理",
                },
            ]
        )
        return {
            "workspace": {
                "id": config.workspace.id,
                "name": config.workspace.name or config.workspace.id,
                "repository": str(config.workspace.repository),
                "version": config.workspace.version,
            },
            "nodes": nodes,
            "edges": edges,
        }

    @app.get("/api/v1/workspaces/{workspace_id}/sessions")
    async def get_workspace_sessions(workspace_id: str) -> list[dict]:
        runtime = await storage.get_runtime(workspace_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        provisioning = await storage.get_provisioning(workspace_id)
        if provisioning is None:
            return []
        if dashboard is None:
            raise HTTPException(status_code=503, detail="session dashboard unavailable")
        try:
            live_sessions = await dashboard.sessions()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"session dashboard read failed: {exc}",
            ) from exc

        config = registry.load_config(workspace_id) if registry is not None else None
        bindings_by_session = {binding.session_id: binding for binding in provisioning.bindings}
        bindings_by_topic = {
            (binding.lark_app_id, binding.chat_id, binding.root_message_id): binding
            for binding in provisioning.bindings
        }
        live_by_id = {str(item.get("sessionId")): item for item in live_sessions}

        def serialize_session(
            session_id: str,
            raw: dict,
            *,
            binding: Any | None,
            registered: bool,
        ) -> dict:
            inferred = binding or bindings_by_topic.get(
                (
                    raw.get("larkAppId"),
                    raw.get("chatId"),
                    raw.get("rootMessageId"),
                )
            )
            agent_id = inferred.agent_id if inferred is not None else None
            agent = config.agents.get(agent_id) if config is not None and agent_id else None
            return {
                "session_id": session_id,
                "agent_id": agent_id,
                "agent_name": agent.display_name if agent is not None else None,
                "status": str(raw.get("status") or "missing"),
                "registered": registered,
                "requires_attention": bool(
                    raw.get("agentAttention")
                    or raw.get("tuiPromptActive")
                    or raw.get("pendingRepo")
                    or raw.get("quarantined")
                ),
                "quarantined": bool(raw.get("quarantined")),
                "working_dir": raw.get("workingDir"),
                "root_message_id": raw.get("rootMessageId")
                or (binding.root_message_id if binding is not None else None),
                "lark_app_id": raw.get("larkAppId")
                or (binding.lark_app_id if binding is not None else None),
                "chat_id": raw.get("chatId")
                or (binding.chat_id if binding is not None else None),
                "updated_at": raw.get("updatedAt") or raw.get("lastActivityAt"),
            }

        result = [
            serialize_session(
                binding.session_id,
                live_by_id.get(binding.session_id, {}),
                binding=binding,
                registered=True,
            )
            for binding in provisioning.bindings
        ]
        workspace_app_ids = {binding.lark_app_id for binding in provisioning.bindings}
        workspace_chat_ids = {binding.chat_id for binding in provisioning.bindings}
        for raw in live_sessions:
            session_id = str(raw.get("sessionId") or "")
            belongs_to_workspace = (
                raw.get("larkAppId") in workspace_app_ids
                and raw.get("chatId") in workspace_chat_ids
            )
            if session_id and session_id not in bindings_by_session and belongs_to_workspace:
                result.append(
                    serialize_session(session_id, raw, binding=None, registered=False)
                )
        return result

    return app
