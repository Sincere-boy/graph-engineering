import asyncio
import os
import uuid
from pathlib import Path

import pytest

from graph_engineering.models import (
    Delivery,
    SessionBinding,
    WorkspaceProvisioning,
    WorkspaceRuntime,
)
from graph_engineering.storage import MongoStorage, SQLiteStorage, Storage


async def assert_storage_contract(storage: Storage) -> None:
    await storage.initialize()
    runtime = WorkspaceRuntime(
        workspace_id="contract-workspace",
        config_version=1,
        config_hash="hash",
        status="running",
        cursor=42,
    )
    delivery = Delivery(
        delivery_id="contract-delivery",
        workspace_id=runtime.workspace_id,
        event_ids=["event-1"],
        target_agent="worker",
        status="delivered",
        message_id="om_contract",
        reconciliation_source="operator_evidence",
    )
    provisioning = WorkspaceProvisioning(
        workspace_id=runtime.workspace_id,
        role_profile_id="profile",
        chat_id="oc_contract",
        bindings=[
            SessionBinding(
                agent_id="worker",
                lark_app_id="cli_worker",
                chat_id="oc_contract",
                root_message_id="om_worker",
                session_id="session-worker",
            )
        ],
    )

    await storage.save_runtime(runtime)
    await storage.save_delivery(delivery)
    await storage.save_provisioning(provisioning)

    assert (await storage.get_runtime(runtime.workspace_id)).model_dump() == runtime.model_dump()
    assert (await storage.get_delivery(delivery.delivery_id)).model_dump() == delivery.model_dump()
    assert [item.delivery_id for item in await storage.list_deliveries(runtime.workspace_id)] == [
        delivery.delivery_id
    ]
    assert (
        await storage.get_provisioning(runtime.workspace_id)
    ).model_dump() == provisioning.model_dump()
    assert await storage.ping() is True


@pytest.mark.asyncio
async def test_sqlite_storage_contract(tmp_path: Path) -> None:
    await assert_storage_contract(SQLiteStorage(tmp_path / "contract.db"))


@pytest.mark.asyncio
async def test_mongodb_storage_contract() -> None:
    uri = os.getenv("GE_TEST_MONGO_URI")
    if not uri:
        pytest.skip("set GE_TEST_MONGO_URI to run the real MongoDB storage contract")
    database = f"graph_engineering_test_{uuid.uuid4().hex}"
    storage = MongoStorage(uri, database)
    try:
        await assert_storage_contract(storage)
    finally:
        await asyncio.to_thread(storage.client.drop_database, database)
        storage.client.close()
