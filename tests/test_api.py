import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_config import valid_config

from graph_engineering.api import create_app
from graph_engineering.config import WorkspaceConfig
from graph_engineering.models import (
    Delivery,
    SessionBinding,
    WorkspaceProvisioning,
)
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import SQLiteStorage


def test_health_and_read_only_workspace_api(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.db")
    app = create_app(storage)

    with TestClient(app) as client:
        assert client.get("/livez").json() == {"status": "alive"}
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v1/workspaces").json() == []
        assert client.post("/api/v1/workspaces").status_code == 405


def test_read_only_delivery_api(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.db")

    async def seed() -> None:
        await storage.initialize()
        await storage.save_delivery(
            Delivery(
                delivery_id="d1",
                workspace_id="ws",
                event_ids=["e1"],
                target_agent="agent",
                status="delivered",
                message_id="om_visible",
            )
        )

    import asyncio

    asyncio.run(seed())
    with TestClient(create_app(storage)) as client:
        response = client.get("/api/v1/workspaces/ws/deliveries")

    assert response.status_code == 200
    assert response.json()[0]["message_id"] == "om_visible"


class DashboardRuntime:
    def __init__(self, sessions: list[dict]):
        self.dashboard = self
        self._sessions = sessions

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def sessions(self) -> list[dict]:
        return self._sessions


def dashboard_app(tmp_path: Path) -> FastAPI:
    storage = SQLiteStorage(tmp_path / "dashboard.db")
    registry = WorkspaceRegistry(tmp_path / "control", storage)
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))

    async def seed() -> None:
        runtime = await registry.register(config)
        runtime.status = "running"
        runtime.active_node = "checker"
        runtime.health = "needs_attention"
        await storage.save_runtime(runtime)
        await storage.save_provisioning(
            WorkspaceProvisioning(
                workspace_id=config.workspace.id,
                role_profile_id="profile",
                chat_id="oc_workspace",
                bindings=[
                    SessionBinding(
                        agent_id="maker",
                        lark_app_id="cli_maker",
                        chat_id="oc_workspace",
                        root_message_id="om_maker",
                        session_id="session-maker",
                    ),
                    SessionBinding(
                        agent_id="checker",
                        lark_app_id="cli_checker",
                        chat_id="oc_workspace",
                        root_message_id="om_checker",
                        session_id="session-checker-missing",
                    ),
                ],
            )
        )

    asyncio.run(seed())
    runtime_service = DashboardRuntime(
        [
            {
                "sessionId": "session-maker",
                "larkAppId": "cli_maker",
                "chatId": "oc_workspace",
                "rootMessageId": "om_maker",
                "status": "working",
                "workingDir": str(tmp_path),
            },
            {
                "sessionId": "session-extra",
                "larkAppId": "cli_maker",
                "chatId": "oc_workspace",
                "rootMessageId": "om_unregistered",
                "status": "idle",
                "agentAttention": True,
                "workingDir": str(tmp_path),
            },
            {
                "sessionId": "other-workspace",
                "larkAppId": "cli_other",
                "chatId": "oc_other",
                "rootMessageId": "om_other",
                "status": "working",
            },
        ]
    )
    return create_app(storage, registry=registry, runtime_service=runtime_service)


def test_dashboard_page_and_assets_are_served(tmp_path: Path) -> None:
    with TestClient(dashboard_app(tmp_path)) as client:
        page = client.get("/")
        script = client.get("/assets/dashboard.js")
        stylesheet = client.get("/assets/dashboard.css")

    assert page.status_code == 200
    assert 'data-app-root' in page.text
    assert "Workspace Graph Console" in page.text
    assert script.status_code == 200
    assert stylesheet.status_code == 200


def test_workspace_graph_marks_the_active_node(tmp_path: Path) -> None:
    with TestClient(dashboard_app(tmp_path)) as client:
        response = client.get("/api/v1/workspaces/arbitrary-flow/graph")

    assert response.status_code == 200
    graph = response.json()
    assert graph["workspace"]["name"] == "arbitrary-flow"
    assert next(node for node in graph["nodes"] if node["id"] == "checker")["active"] is True
    assert next(node for node in graph["nodes"] if node["id"] == "maker")["active"] is False
    assert {
        "source": "maker",
        "target": "checker",
        "state_id": "inspect",
        "label": "进入检查",
    } in graph["edges"]


def test_workspace_sessions_include_missing_and_unregistered_sessions(tmp_path: Path) -> None:
    with TestClient(dashboard_app(tmp_path)) as client:
        response = client.get("/api/v1/workspaces/arbitrary-flow/sessions")

    assert response.status_code == 200
    sessions = {session["session_id"]: session for session in response.json()}
    assert sessions["session-maker"]["status"] == "working"
    assert sessions["session-maker"]["agent_id"] == "maker"
    assert sessions["session-maker"]["registered"] is True
    assert sessions["session-checker-missing"]["status"] == "missing"
    assert sessions["session-extra"]["registered"] is False
    assert sessions["session-extra"]["requires_attention"] is True
    assert "other-workspace" not in sessions
