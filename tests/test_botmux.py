import asyncio
import json

import httpx
import pytest

from graph_engineering.botmux import BotmuxClient, BotmuxError, DeliveryUncertain


def request_body(request: httpx.Request) -> dict:
    return json.loads(request.content)


class FakeProcess:
    returncode = 0

    def __init__(self) -> None:
        self.input: bytes | None = None

    async def communicate(self, input: bytes | None = None):
        self.input = input
        return (
            json.dumps(
                {
                    "success": True,
                    "messageId": "om_visible",
                    "sessionId": "s1",
                }
            ).encode(),
            b"sent",
        )


class HangingProcess:
    returncode = None

    def __init__(self) -> None:
        self.killed = False

    async def communicate(self, input: bytes | None = None):
        await asyncio.Event().wait()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_send_session_message_uses_registered_organizer_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_exec(*args, **kwargs):
        seen.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BotmuxClient("http://botmux.test")

    result = await client.send_session_message(
        session_id="s1",
        content="请人工确认",
        delivery_id="delivery-1",
    )

    assert result == "om_visible"
    args, kwargs = seen[0]
    assert args == (
        "botmux",
        "send",
        "--session-id",
        "s1",
        "--no-mention",
    )
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert process.input == "请人工确认".encode()


@pytest.mark.asyncio
async def test_send_session_message_uses_configured_botmux_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    seen: list[tuple[object, ...]] = []

    async def fake_exec(*args, **kwargs):
        seen.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BotmuxClient(
        "http://botmux.test",
        botmux_command="/opt/botmux/bin/botmux",
    )

    await client.send_session_message(
        session_id="s1",
        content="请人工确认",
        delivery_id="delivery-1",
    )

    assert seen[0][0] == "/opt/botmux/bin/botmux"


@pytest.mark.asyncio
async def test_send_session_message_reports_missing_botmux_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BotmuxClient("http://botmux.test", botmux_command="/missing/botmux")

    with pytest.raises(BotmuxError, match="executable is unavailable"):
        await client.send_session_message(
            session_id="s1",
            content="请人工确认",
            delivery_id="delivery-1",
        )


@pytest.mark.asyncio
async def test_send_session_message_times_out_as_uncertain_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HangingProcess()

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = BotmuxClient("http://botmux.test", send_timeout=0.01)

    with pytest.raises(DeliveryUncertain, match="timed out"):
        await client.send_session_message(
            session_id="s1",
            content="请人工确认",
            delivery_id="delivery-1",
        )

    assert process.killed is True


@pytest.mark.asyncio
async def test_trigger_uses_existing_session_and_stable_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "target": {"sessionId": "s1"}})
        return httpx.Response(
            200, json={"ok": True, "state": "completed", "output": {"content": "ok"}}
        )

    client = BotmuxClient(
        "http://botmux.test",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    result = await client.trigger_session(
        bot_id="bot1",
        session_id="s1",
        instruction="trusted",
        payload={"events": ["e1"]},
        idempotency_key="ws:v1:e1",
    )

    body = request_body(seen[0])
    assert body["target"] == {"kind": "turn", "botId": "bot1", "sessionId": "s1"}
    assert body["envelope"]["trusted"] is False
    assert body["options"]["turnIdempotencyKey"] == "ws:v1:e1"
    assert "idempotencyKey" not in body["options"]
    assert result == "ok"


@pytest.mark.asyncio
async def test_trigger_marks_ambiguous_transport_failure_for_reconciliation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unknown commit state")

    client = BotmuxClient("http://botmux.test", transport=httpx.MockTransport(handler))

    with pytest.raises(DeliveryUncertain):
        await client.trigger_session(
            bot_id="bot1",
            session_id="s1",
            instruction="trusted",
            payload={},
            idempotency_key="key",
        )


@pytest.mark.asyncio
async def test_client_ignores_environment_proxy_for_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://invalid::")

    client = BotmuxClient("http://127.0.0.1:7891")
    await client.close()
