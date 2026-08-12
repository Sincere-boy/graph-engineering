from pathlib import Path

import pytest

from graph_engineering.models import (
    Delivery,
    SessionBinding,
    WorkspaceProvisioning,
    WorkspaceRuntime,
)
from graph_engineering.storage import SQLiteStorage, create_storage


@pytest.mark.asyncio
async def test_sqlite_persists_runtime_cursor_and_outbox(tmp_path: Path) -> None:
    db = SQLiteStorage(tmp_path / "state.db")
    await db.initialize()
    runtime = WorkspaceRuntime(
        workspace_id="ws",
        config_version=1,
        config_hash="abc",
        status="running",
        cursor=17,
    )
    delivery = Delivery(
        delivery_id="d1",
        workspace_id="ws",
        event_ids=["e1"],
        target_agent="target",
        status="pending",
    )

    await db.save_runtime(runtime)
    await db.save_delivery(delivery)

    assert (await db.get_runtime("ws")).cursor == 17
    assert (await db.get_delivery("d1")).status == "pending"
    assert [item.delivery_id for item in await db.pending_deliveries("ws")] == ["d1"]
    assert [item.delivery_id for item in await db.list_deliveries("ws")] == ["d1"]

    provisioning = WorkspaceProvisioning(
        workspace_id="ws",
        role_profile_id="profile-ws",
        chat_id="oc_ws",
        bindings=[
            SessionBinding(
                agent_id="target",
                lark_app_id="cli_target",
                chat_id="oc_ws",
                root_message_id="om_target",
                session_id="session-target",
            )
        ],
    )
    await db.save_provisioning(provisioning)
    assert (await db.get_provisioning("ws")).bindings[0].session_id == "session-target"


def test_storage_backend_is_explicit_and_never_silently_falls_back(tmp_path: Path) -> None:
    assert isinstance(create_storage("sqlite", sqlite_path=tmp_path / "db.sqlite"), SQLiteStorage)

    with pytest.raises(ValueError, match="backend"):
        create_storage("automatic", sqlite_path=tmp_path / "db.sqlite")
