from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Protocol

from pymongo import ASCENDING, MongoClient

from graph_engineering.models import Delivery, WorkspaceProvisioning, WorkspaceRuntime


class Storage(Protocol):
    async def initialize(self) -> None: ...
    async def ping(self) -> bool: ...
    async def save_runtime(self, runtime: WorkspaceRuntime) -> None: ...
    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntime | None: ...
    async def list_runtimes(self) -> list[WorkspaceRuntime]: ...
    async def save_delivery(self, delivery: Delivery) -> None: ...
    async def get_delivery(self, delivery_id: str) -> Delivery | None: ...
    async def list_deliveries(self, workspace_id: str) -> list[Delivery]: ...
    async def pending_deliveries(self, workspace_id: str) -> list[Delivery]: ...
    async def save_provisioning(self, provisioning: WorkspaceProvisioning) -> None: ...
    async def get_provisioning(self, workspace_id: str) -> WorkspaceProvisioning | None: ...


class SQLiteStorage:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        async with self._lock:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS runtimes "
                    "(workspace_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS deliveries "
                    "(delivery_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                    "status TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS deliveries_pending "
                    "ON deliveries(workspace_id, status)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS provisioning "
                    "(workspace_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )

    async def ping(self) -> bool:
        try:
            async with self._lock:
                with self._connect() as db:
                    db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    async def save_runtime(self, runtime: WorkspaceRuntime) -> None:
        payload = runtime.model_dump_json()
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO runtimes(workspace_id, payload) VALUES (?, ?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET payload=excluded.payload",
                    (runtime.workspace_id, payload),
                )

    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntime | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT payload FROM runtimes WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
        return WorkspaceRuntime.model_validate_json(row["payload"]) if row else None

    async def list_runtimes(self) -> list[WorkspaceRuntime]:
        async with self._lock:
            with self._connect() as db:
                rows = db.execute("SELECT payload FROM runtimes ORDER BY workspace_id").fetchall()
        return [WorkspaceRuntime.model_validate_json(row["payload"]) for row in rows]

    async def save_delivery(self, delivery: Delivery) -> None:
        payload = delivery.model_dump_json()
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO deliveries(delivery_id, workspace_id, status, payload) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(delivery_id) DO UPDATE SET "
                    "status=excluded.status, payload=excluded.payload",
                    (delivery.delivery_id, delivery.workspace_id, delivery.status, payload),
                )

    async def get_delivery(self, delivery_id: str) -> Delivery | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT payload FROM deliveries WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
        return Delivery.model_validate_json(row["payload"]) if row else None

    async def pending_deliveries(self, workspace_id: str) -> list[Delivery]:
        async with self._lock:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT payload FROM deliveries WHERE workspace_id=? "
                    "AND status='pending' ORDER BY delivery_id",
                    (workspace_id,),
                ).fetchall()
        return [Delivery.model_validate_json(row["payload"]) for row in rows]

    async def list_deliveries(self, workspace_id: str) -> list[Delivery]:
        async with self._lock:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT payload FROM deliveries WHERE workspace_id=? "
                    "ORDER BY delivery_id",
                    (workspace_id,),
                ).fetchall()
        return [Delivery.model_validate_json(row["payload"]) for row in rows]

    async def save_provisioning(self, provisioning: WorkspaceProvisioning) -> None:
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO provisioning(workspace_id, payload) VALUES (?, ?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET payload=excluded.payload",
                    (provisioning.workspace_id, provisioning.model_dump_json()),
                )

    async def get_provisioning(self, workspace_id: str) -> WorkspaceProvisioning | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute(
                    "SELECT payload FROM provisioning WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
        return WorkspaceProvisioning.model_validate_json(row["payload"]) if row else None


class MongoStorage:
    def __init__(self, uri: str, database: str = "graph_engineering"):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.db = self.client[database]

    async def initialize(self) -> None:
        await asyncio.to_thread(self.client.admin.command, "ping")
        await asyncio.to_thread(
            self.db.deliveries.create_index,
            [("workspace_id", ASCENDING), ("status", ASCENDING)],
        )

    async def ping(self) -> bool:
        try:
            await asyncio.to_thread(self.client.admin.command, "ping")
            return True
        except Exception:
            return False

    async def save_runtime(self, runtime: WorkspaceRuntime) -> None:
        payload = runtime.model_dump(mode="json")
        await asyncio.to_thread(
            self.db.runtimes.replace_one,
            {"workspace_id": runtime.workspace_id},
            payload,
            upsert=True,
        )

    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntime | None:
        row = await asyncio.to_thread(self.db.runtimes.find_one, {"workspace_id": workspace_id})
        return WorkspaceRuntime.model_validate(row) if row else None

    async def list_runtimes(self) -> list[WorkspaceRuntime]:
        rows = await asyncio.to_thread(
            lambda: list(self.db.runtimes.find().sort("workspace_id", 1))
        )
        return [WorkspaceRuntime.model_validate(row) for row in rows]

    async def save_delivery(self, delivery: Delivery) -> None:
        payload = delivery.model_dump(mode="json")
        await asyncio.to_thread(
            self.db.deliveries.replace_one,
            {"delivery_id": delivery.delivery_id},
            payload,
            upsert=True,
        )

    async def get_delivery(self, delivery_id: str) -> Delivery | None:
        row = await asyncio.to_thread(self.db.deliveries.find_one, {"delivery_id": delivery_id})
        return Delivery.model_validate(row) if row else None

    async def pending_deliveries(self, workspace_id: str) -> list[Delivery]:
        rows = await asyncio.to_thread(
            lambda: list(
                self.db.deliveries.find({"workspace_id": workspace_id, "status": "pending"}).sort(
                    "delivery_id", 1
                )
            )
        )
        return [Delivery.model_validate(row) for row in rows]

    async def list_deliveries(self, workspace_id: str) -> list[Delivery]:
        rows = await asyncio.to_thread(
            lambda: list(
                self.db.deliveries.find({"workspace_id": workspace_id}).sort("delivery_id", 1)
            )
        )
        return [Delivery.model_validate(row) for row in rows]

    async def save_provisioning(self, provisioning: WorkspaceProvisioning) -> None:
        payload = provisioning.model_dump(mode="json")
        await asyncio.to_thread(
            self.db.provisioning.replace_one,
            {"workspace_id": provisioning.workspace_id},
            payload,
            upsert=True,
        )

    async def get_provisioning(self, workspace_id: str) -> WorkspaceProvisioning | None:
        row = await asyncio.to_thread(self.db.provisioning.find_one, {"workspace_id": workspace_id})
        return WorkspaceProvisioning.model_validate(row) if row else None


def create_storage(
    backend: str,
    *,
    sqlite_path: Path | None = None,
    mongo_uri: str | None = None,
    mongo_database: str = "graph_engineering",
) -> Storage:
    if backend == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite backend requires sqlite_path")
        return SQLiteStorage(sqlite_path)
    if backend == "mongodb":
        if not mongo_uri:
            raise ValueError("mongodb backend requires mongo_uri")
        return MongoStorage(mongo_uri, mongo_database)
    raise ValueError("storage backend must be explicitly 'sqlite' or 'mongodb'")


def storage_from_environment(control_dir: Path) -> Storage:
    """Use MongoDB by default; SQLite is enabled only by an explicit setting."""
    return create_storage(
        os.getenv("GE_STORAGE_BACKEND", "mongodb"),
        sqlite_path=Path(os.getenv("GE_SQLITE_PATH", str(control_dir / "state.db"))),
        mongo_uri=os.getenv("GE_MONGO_URI", "mongodb://127.0.0.1:27017"),
        mongo_database=os.getenv("GE_MONGO_DATABASE", "graph_engineering"),
    )
