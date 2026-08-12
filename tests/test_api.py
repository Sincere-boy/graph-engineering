from pathlib import Path

from fastapi.testclient import TestClient

from graph_engineering.api import create_app
from graph_engineering.storage import SQLiteStorage


def test_health_and_read_only_workspace_api(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.db")
    app = create_app(storage)

    with TestClient(app) as client:
        assert client.get("/livez").json() == {"status": "alive"}
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v1/workspaces").json() == []
        assert client.post("/api/v1/workspaces").status_code == 405
