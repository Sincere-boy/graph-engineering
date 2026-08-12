from __future__ import annotations

import asyncio
from typing import Any

import httpx


class DeliveryUncertain(RuntimeError):
    pass


class BotmuxError(RuntimeError):
    pass


class BotmuxClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval: float = 2,
        max_polls: int = 900,
    ):
        cookies = {"botmux_dashboard_token": token} if token else None
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            cookies=cookies,
            transport=transport,
            timeout=30,
            trust_env=False,
        )
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    async def close(self) -> None:
        await self.client.aclose()

    async def trigger_session(
        self,
        *,
        bot_id: str,
        session_id: str,
        instruction: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        body = {
            "source": {"type": "workflow", "requestId": idempotency_key},
            "target": {"kind": "turn", "botId": bot_id, "sessionId": session_id},
            "instruction": instruction,
            "envelope": {
                "format": "graph-engineering.v1",
                "sourceName": "graph-engineering",
                "trusted": False,
                "payload": payload,
            },
            "options": {
                "turnIdempotencyKey": idempotency_key,
                "asyncReturnSessionId": True,
                "waitForFinalOutput": False,
            },
        }
        try:
            response = await self.client.post("/api/trigger", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise DeliveryUncertain(f"botmux trigger commit state is unknown: {exc}") from exc
        if response.status_code >= 500:
            raise DeliveryUncertain(
                f"botmux trigger returned ambiguous HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise BotmuxError(
                f"botmux trigger rejected: HTTP {response.status_code} {response.text}"
            )
        data = response.json()
        result_session = (data.get("target") or {}).get("sessionId") or session_id
        for _ in range(self.max_polls):
            try:
                polled = await self.client.get(f"/api/sessions/{result_session}/trigger-result")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise DeliveryUncertain(f"botmux result state is unknown: {exc}") from exc
            if polled.status_code >= 500:
                raise DeliveryUncertain(f"botmux result returned HTTP {polled.status_code}")
            result = polled.json()
            state = result.get("state")
            if state == "completed":
                return str((result.get("output") or {}).get("content", ""))
            if state in {"failed", "not_found"}:
                raise BotmuxError(f"botmux trigger ended in state {state}: {result}")
            await asyncio.sleep(self.poll_interval)
        raise DeliveryUncertain("botmux trigger did not reach a terminal state before poll limit")

    async def dashboard_summary(self) -> dict[str, Any]:
        response = await self.client.get("/api/dashboard/v1/summary")
        response.raise_for_status()
        return response.json()

    async def sessions(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/sessions")
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else payload.get("sessions", [])
