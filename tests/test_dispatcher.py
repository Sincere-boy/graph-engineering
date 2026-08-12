from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.botmux import DeliveryUncertain
from graph_engineering.config import WorkspaceConfig
from graph_engineering.dispatcher import BotmuxDispatcher
from graph_engineering.engine import StateGraphEngine
from graph_engineering.models import Delivery, Event, SessionBinding, WorkspaceProvisioning


class FakeBotmux:
    def __init__(self, output: str):
        self.output = output
        self.calls = []

    async def trigger_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


@pytest.mark.asyncio
async def test_dispatcher_uses_organizer_session_and_declared_target(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="e1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="maker",
        state_id="inspect",
        message="inspect",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="d1",
        workspace_id=config.workspace.id,
        event_ids=["e1"],
        target_agent="checker",
        status="pending",
    )
    provisioning = WorkspaceProvisioning(
        workspace_id=config.workspace.id,
        role_profile_id="p1",
        chat_id="oc1",
        bindings=[
            SessionBinding(
                agent_id="organizer",
                lark_app_id="cli_org",
                chat_id="oc1",
                root_message_id="om_org",
                session_id="s_org",
            ),
            SessionBinding(
                agent_id="checker",
                lark_app_id="cli_check",
                chat_id="oc1",
                root_message_id="om_check",
                session_id="s_check",
            ),
        ],
    )
    botmux = FakeBotmux('{"delivery_id":"d1","message_id":"om_visible"}')
    dispatcher = BotmuxDispatcher(botmux, lambda _: provisioning)

    result = await dispatcher.dispatch(delivery, decision, [event], config)

    assert result == "om_visible"
    call = botmux.calls[0]
    assert call["bot_id"] == "cli_org"
    assert call["session_id"] == "s_org"
    assert "om_check" in call["instruction"]
    assert "cli_check" in call["instruction"]
    assert "禁止 `botmux report`" in call["instruction"]
    assert "禁止带 `--title`" in call["instruction"]
    assert (
        '只输出单行 JSON：{"delivery_id":"d1","message_id":"<botmux返回的消息ID>"}。'
        in call["instruction"]
    )
    assert call["payload"]["events"][0]["message"] == "inspect"


@pytest.mark.asyncio
async def test_dispatcher_treats_invalid_organizer_receipt_as_uncertain(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="e1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="maker",
        state_id="inspect",
        message="inspect",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="d1",
        workspace_id=config.workspace.id,
        event_ids=["e1"],
        target_agent="checker",
        status="pending",
    )
    provisioning = WorkspaceProvisioning(
        workspace_id=config.workspace.id,
        role_profile_id="p1",
        chat_id="oc1",
        bindings=[
            SessionBinding(
                agent_id="organizer",
                lark_app_id="cli_org",
                chat_id="oc1",
                root_message_id="om_org",
                session_id="s_org",
            ),
            SessionBinding(
                agent_id="checker",
                lark_app_id="cli_check",
                chat_id="oc1",
                root_message_id="om_check",
                session_id="s_check",
            ),
        ],
    )

    with pytest.raises(DeliveryUncertain, match="receipt"):
        await BotmuxDispatcher(FakeBotmux("done maybe"), lambda _: provisioning).dispatch(
            delivery, decision, [event], config
        )

    with pytest.raises(DeliveryUncertain, match="message id"):
        await BotmuxDispatcher(
            FakeBotmux('{"delivery_id":"d1","message_id":"not-a-feishu-message"}'),
            lambda _: provisioning,
        ).dispatch(delivery, decision, [event], config)


@pytest.mark.asyncio
async def test_dispatcher_recovery_uses_organizer_and_fixed_active_agent_topic(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    provisioning = WorkspaceProvisioning(
        workspace_id=config.workspace.id,
        role_profile_id="p1",
        chat_id="oc1",
        bindings=[
            SessionBinding(
                agent_id="organizer",
                lark_app_id="cli_org",
                chat_id="oc1",
                root_message_id="om_org",
                session_id="s_org",
            ),
            SessionBinding(
                agent_id="maker",
                lark_app_id="cli_maker",
                chat_id="oc1",
                root_message_id="om_maker",
                session_id="s_maker",
            ),
        ],
    )
    events = [
        Event(
            event_id=f"e{index}",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="begin",
            message=f"event {index}",
        )
        for index in range(5)
    ]
    delivery = Delivery(
        delivery_id="recovery-d1",
        workspace_id=config.workspace.id,
        event_ids=[event.event_id for event in events],
        target_agent="maker",
        status="pending",
        kind="recovery",
        source_cursor=123,
        attempt=1,
    )
    botmux = FakeBotmux('{"delivery_id":"recovery-d1","message_id":"om_recovered"}')

    result = await BotmuxDispatcher(botmux, lambda _: provisioning).recover(
        delivery, "maker", events, config
    )

    assert result == "om_recovered"
    call = botmux.calls[0]
    assert call["bot_id"] == "cli_org"
    assert call["session_id"] == "s_org"
    assert call["idempotency_key"] == "recovery-d1"
    assert "异常恢复模式" in call["instruction"]
    assert "om_maker" in call["instruction"]
    assert "不得改变目标" in call["instruction"]
    assert [event["event_id"] for event in call["payload"]["recent_events"]] == [
        "e0",
        "e1",
        "e2",
        "e3",
        "e4",
    ]
