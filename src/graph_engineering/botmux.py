from __future__ import annotations

import asyncio
import json
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
        send_timeout: float = 60,
        botmux_command: str = "botmux",
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
        self.send_timeout = send_timeout
        self.botmux_command = botmux_command

    async def close(self) -> None:
        await self.client.aclose()

    async def send_session_message(
        self,
        *,
        session_id: str,
        content: str,
        delivery_id: str,
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.botmux_command,
                "send",
                "--session-id",
                session_id,
                "--no-mention",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BotmuxError(
                f"botmux executable is unavailable: {self.botmux_command}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(content.encode()), timeout=self.send_timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DeliveryUncertain(
                f"botmux send timed out with an unknown result for {delivery_id}"
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise DeliveryUncertain(
                f"botmux send result is uncertain for {delivery_id}: {detail}"
            )
        try:
            receipt = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryUncertain(
                f"botmux send returned an invalid receipt for {delivery_id}"
            ) from exc
        message_id = receipt.get("messageId") if isinstance(receipt, dict) else None
        if not isinstance(message_id, str) or not message_id.startswith("om_"):
            raise DeliveryUncertain(
                f"botmux send omitted a valid Feishu message id for {delivery_id}"
            )
        return message_id

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
