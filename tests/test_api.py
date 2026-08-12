from pathlib import Path

from fastapi.testclient import TestClient

from graph_engineering.api import create_app
from graph_engineering.models import Delivery
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
