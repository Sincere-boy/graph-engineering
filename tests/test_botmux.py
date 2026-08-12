import httpx
import pytest

from graph_engineering.botmux import BotmuxClient, DeliveryUncertain


def request_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


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
