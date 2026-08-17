from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.botmux import BotmuxError, DeliveryUncertain
from graph_engineering.config import WorkspaceConfig
from graph_engineering.dispatcher import BotmuxDispatcher
from graph_engineering.engine import StateGraphEngine
from graph_engineering.models import Delivery, Event, SessionBinding, WorkspaceProvisioning


class FakeBotmux:
    def __init__(self, output: str, *, send_output: str = "om_direct"):
        self.output = output
        self.send_output = send_output
        self.calls = []
        self.send_calls = []

    async def trigger_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.output

    async def send_session_message(self, **kwargs):
        self.send_calls.append(kwargs)
        return self.send_output


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
async def test_dispatcher_notifies_user_in_group_when_workflow_completes(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="done-1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="checker",
        state_id="done",
        message="所有验收测试通过，报告已生成",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="completion-d1",
        workspace_id=config.workspace.id,
        event_ids=[event.event_id],
        target_agent=None,
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
                session_scope="group",
            )
        ],
    )
    botmux = FakeBotmux(
        '{"delivery_id":"completion-d1","content":"工作区已完成，所有验收测试通过。"}',
        send_output="om_completion",
    )

    result = await BotmuxDispatcher(botmux, lambda _: provisioning).dispatch(
        delivery, decision, [event], config
    )

    assert result == "om_completion"
    call = botmux.calls[0]
    assert call["bot_id"] == "cli_org"
    assert call["session_id"] == "s_org"
    assert call["payload"]["mode"] == "completion_notification"
    assert "只生成通知正文" in call["instruction"]
    assert "botmux send" not in call["instruction"]
    assert "botmux dispatch --into" not in call["instruction"]
    send_call = botmux.send_calls[0]
    assert send_call == {
        "session_id": "s_org",
        "content": "工作区已完成，所有验收测试通过。",
        "delivery_id": "completion-d1",
    }


@pytest.mark.asyncio
async def test_dispatcher_does_not_send_invalid_completion_content(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="done-1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="checker",
        state_id="done",
        message="所有验收测试通过",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="completion-d1",
        workspace_id=config.workspace.id,
        event_ids=[event.event_id],
        target_agent=None,
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
                session_scope="group",
            )
        ],
    )
    botmux = FakeBotmux('{"delivery_id":"completion-d1","content":""}')

    with pytest.raises(BotmuxError, match="completion content"):
        await BotmuxDispatcher(botmux, lambda _: provisioning).dispatch(
            delivery, decision, [event], config
        )

    assert botmux.send_calls == []


@pytest.mark.asyncio
async def test_dispatcher_notifies_human_in_group_without_self_dispatch(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="human-1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="maker",
        state_id="human_required",
        message="请人工确认验收结果",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="human-d1",
        workspace_id=config.workspace.id,
        event_ids=[event.event_id],
        target_agent="organizer",
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
                root_message_id="oc1",
                session_id="s_org",
                session_scope="group",
            )
        ],
    )
    botmux = FakeBotmux("", send_output="om_human_notification")

    result = await BotmuxDispatcher(botmux, lambda _: provisioning).dispatch(
        delivery, decision, [event], config
    )

    assert result == "om_human_notification"
    assert botmux.calls == []
    call = botmux.send_calls[0]
    assert call["session_id"] == "s_org"
    assert call["delivery_id"] == "human-d1"
    assert "待人工处理" in call["content"]
    assert "请人工确认验收结果" in call["content"]
    assert config.workspace.id in call["content"]


@pytest.mark.asyncio
async def test_dispatcher_notifies_closed_status_in_group_without_self_dispatch(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    event = Event(
        event_id="closed-1",
        workspace_id=config.workspace.id,
        config_version=1,
        actor_id="organizer",
        state_id="closed",
        message="关闭工作区",
    )
    decision = StateGraphEngine(config).decide([event])
    delivery = Delivery(
        delivery_id="closed-d1",
        workspace_id=config.workspace.id,
        event_ids=[event.event_id],
        target_agent="organizer",
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
                root_message_id="oc1",
                session_id="s_org",
                session_scope="group",
            )
        ],
    )
    botmux = FakeBotmux(
        '{"delivery_id":"closed-d1","message_id":"om_closed_notification"}'
    )

    result = await BotmuxDispatcher(botmux, lambda _: provisioning).dispatch(
        delivery, decision, [event], config
    )

    assert result == "om_closed_notification"
    call = botmux.calls[0]
    assert call["payload"]["mode"] == "status_notification"
    assert "botmux send --no-mention" in call["instruction"]
    assert "botmux dispatch --into" not in call["instruction"]


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
