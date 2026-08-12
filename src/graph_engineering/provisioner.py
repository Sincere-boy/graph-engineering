from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

import httpx

from graph_engineering.botmux import BotmuxError, DeliveryUncertain
from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.models import SessionBinding, WorkspaceProvisioning
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import Storage


class BotmuxAdmin(Protocol):
    async def create_bot(self, *, name: str, working_dir: str, cli_id: str) -> str: ...

    async def put_role_profile(self, profile_id: str, app_id: str, content: str) -> None: ...

    async def create_group(
        self, *, name: str, app_ids: list[str], working_dir: str, profile_id: str
    ) -> str: ...

    async def create_topic(
        self,
        *,
        app_id: str,
        chat_id: str,
        title: str,
        instruction: str,
        idempotency_key: str,
    ) -> tuple[str, str]: ...


class BotmuxAdminClient:
    """Documented botmux Dashboard API adapter; never reads botmux private formats."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        poll_interval: float = 2,
        max_polls: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            cookies={"botmux_dashboard_token": token},
            timeout=60,
            transport=transport,
            trust_env=False,
        )
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    async def close(self) -> None:
        await self.client.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise DeliveryUncertain(f"botmux admin operation has unknown result: {exc}") from exc
        if response.status_code >= 500:
            raise DeliveryUncertain(
                f"botmux admin operation has ambiguous HTTP {response.status_code}: {response.text}"
            )
        if response.status_code >= 400:
            raise BotmuxError(
                f"botmux admin operation rejected: HTTP {response.status_code} {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BotmuxError("botmux admin response is not an object")
        return payload

    async def create_bot(self, *, name: str, working_dir: str, cli_id: str) -> str:
        options = await self._json("GET", "/api/cli-options")
        session = options.get("webSession") or {}
        identity = session.get("identity") or {}
        if (
            session.get("status") != "ready"
            or not identity.get("userId")
            or not identity.get("tenantId")
        ):
            raise BotmuxError("botmux Feishu web session is not ready for no-scan onboarding")
        started = await self._json(
            "POST",
            "/api/bot-onboarding/start",
            json={
                "appName": name,
                "registrationMode": "web",
                "sessionMode": "reuse",
                "expectedIdentity": {
                    "userId": identity["userId"],
                    "tenantId": identity["tenantId"],
                },
                "cliId": cli_id,
                "workingDir": working_dir,
                "dirMode": "fixed",
                "requireCriticalScopesBeforeActivation": True,
            },
        )
        job = started.get("job") or {}
        job_id = str(job.get("id") or "")
        if not job_id:
            raise BotmuxError("botmux onboarding did not return a job id")
        owner_submitted = False
        for _ in range(self.max_polls):
            snapshot = (await self._json("GET", f"/api/bot-onboarding/{job_id}")).get("job") or {}
            status = snapshot.get("status")
            if status == "completed":
                app_id = str(snapshot.get("appId") or "")
                if not app_id:
                    raise BotmuxError("completed botmux onboarding omitted appId")
                await self._wait_online(app_id)
                return app_id
            if status == "needs_owner" and not owner_submitted:
                owner = snapshot.get("suggestedOwner") or identity.get("email")
                if not owner:
                    raise BotmuxError(
                        "botmux onboarding requires an owner but has no verified owner"
                    )
                await self._json(
                    "POST", f"/api/bot-onboarding/{job_id}/owner", json={"owner": owner}
                )
                owner_submitted = True
            elif status in {"waiting_for_scan", "waiting_for_platform_scan"}:
                raise BotmuxError(
                    "botmux unexpectedly requires an interactive scan; "
                    "cached identity cannot complete onboarding"
                )
            elif status == "failed":
                raise BotmuxError(f"botmux onboarding failed: {snapshot.get('error') or snapshot}")
            await asyncio.sleep(self.poll_interval)
        raise DeliveryUncertain("botmux onboarding did not finish before poll limit")

    async def _wait_online(self, app_id: str) -> None:
        for _ in range(self.max_polls):
            payload = await self._json("GET", "/api/bots")
            bot = next(
                (item for item in payload.get("bots", []) if item.get("larkAppId") == app_id), None
            )
            if bot and bot.get("online") is True:
                return
            await asyncio.sleep(self.poll_interval)
        raise DeliveryUncertain(f"new bot {app_id} was created but did not become online")

    async def put_role_profile(self, profile_id: str, app_id: str, content: str) -> None:
        payload = await self._json(
            "PUT",
            f"/api/role-profiles/{profile_id}/{app_id}",
            json={"content": content, "allowEmpty": True},
        )
        if payload.get("ok") is False:
            raise BotmuxError(f"role profile write failed: {payload}")

    async def create_group(
        self, *, name: str, app_ids: list[str], working_dir: str, profile_id: str
    ) -> str:
        payload = await self._json(
            "POST",
            "/api/groups/create",
            json={
                "name": name,
                "larkAppIds": app_ids,
                "bindWorkingDir": working_dir,
                "roleProfileId": profile_id,
            },
        )
        chat_id = str(payload.get("chatId") or "")
        if payload.get("ok") is False or not chat_id:
            raise BotmuxError(f"group creation failed: {payload}")
        return chat_id

    async def create_topic(
        self,
        *,
        app_id: str,
        chat_id: str,
        title: str,
        instruction: str,
        idempotency_key: str,
    ) -> tuple[str, str]:
        payload = await self._json(
            "POST",
            "/api/trigger",
            json={
                "source": {"type": "workflow", "requestId": idempotency_key},
                "target": {"kind": "turn", "botId": app_id, "chatId": chat_id},
                "instruction": instruction,
                "envelope": {
                    "format": "graph-engineering.provision.v1",
                    "sourceName": "graph-engineering",
                    "trusted": False,
                    "payload": {"purpose": "initialize-agent-topic"},
                },
                "presentation": {"topicMessage": title[:200]},
                "options": {
                    "idempotencyKey": idempotency_key,
                    "asyncReturnSessionId": True,
                    "waitForFinalOutput": False,
                },
            },
        )
        session_id = str((payload.get("target") or {}).get("sessionId") or "")
        if not session_id:
            raise BotmuxError(f"topic trigger omitted session id: {payload}")
        await self._poll_trigger(session_id)
        sessions = await self._json("GET", "/api/sessions")
        items = sessions.get("sessions", [])
        session = next((item for item in items if item.get("sessionId") == session_id), None)
        root_message_id = str((session or {}).get("rootMessageId") or "")
        if not root_message_id:
            raise BotmuxError(f"session {session_id} omitted rootMessageId")
        return root_message_id, session_id

    async def _poll_trigger(self, session_id: str) -> str:
        for _ in range(self.max_polls):
            payload = await self._json("GET", f"/api/sessions/{session_id}/trigger-result")
            state = payload.get("state")
            if state == "completed":
                return str((payload.get("output") or {}).get("content") or "")
            if state in {"failed", "not_found"}:
                raise BotmuxError(f"botmux trigger ended in state {state}: {payload}")
            await asyncio.sleep(self.poll_interval)
        raise DeliveryUncertain("botmux trigger did not finish before poll limit")


class Provisioner:
    def __init__(self, control_dir: Path, admin: BotmuxAdmin, storage: Storage | None = None):
        self.control_dir = control_dir
        self.admin = admin
        self.storage = storage

    async def provision(
        self,
        config: WorkspaceConfig,
        *,
        cli_id: str = "codex",
        name_prefix: str = "GE",
    ) -> WorkspaceProvisioning:
        if self.storage is not None:
            await self.storage.initialize()
            existing = await self.storage.get_provisioning(config.workspace.id)
            if existing is not None:
                return existing
        checkpoint_path = (
            self.control_dir / "workspaces" / config.workspace.id / "provision-checkpoint.json"
        )
        checkpoint = self._load_checkpoint(checkpoint_path, config)
        if checkpoint.get("provisioning"):
            return WorkspaceProvisioning.model_validate(checkpoint["provisioning"])
        apps: dict[str, str] = checkpoint.setdefault("apps", {})
        profile_id = str(checkpoint.setdefault("profile_id", f"graph-{config.workspace.id}"))
        registry = (
            WorkspaceRegistry(self.control_dir, storage=self.storage)
            if self.storage
            else WorkspaceRegistry(self.control_dir)
        )

        ordered_agents = ["organizer", *(key for key in config.agents if key != "organizer")]
        for agent_id in ordered_agents:
            agent = config.agents[agent_id]
            if agent_id not in apps:
                display = agent.display_name.replace("/", "-")
                name = f"{name_prefix}-{config.workspace.id}-{display}"[:64]
                apps[agent_id] = await self.admin.create_bot(
                    name=name,
                    working_dir=str(config.workspace.repository),
                    cli_id=cli_id,
                )
                self._save_checkpoint(checkpoint_path, checkpoint)

        profiled: list[str] = checkpoint.setdefault("profiled", [])
        diagram = registry.render_mermaid(config)
        for agent_id in ordered_agents:
            app_id = apps[agent_id]
            if agent_id in profiled:
                continue
            prompt = registry.render_agent_prompt(config, agent_id)
            if agent_id == "organizer":
                prompt += (
                    f"\n\n当前冻结配置哈希：`{config.content_hash}`\n\n```mermaid\n{diagram}```"
                )
            await self.admin.put_role_profile(profile_id, app_id, prompt)
            profiled.append(agent_id)
            self._save_checkpoint(checkpoint_path, checkpoint)

        chat_id = str(checkpoint.get("chat_id") or "")
        if not chat_id:
            chat_id = await self.admin.create_group(
                name=f"Graph Engineering · {config.workspace.name or config.workspace.id}"[:100],
                app_ids=list(apps.values()),
                working_dir=str(config.workspace.repository),
                profile_id=profile_id,
            )
            checkpoint["chat_id"] = chat_id
            self._save_checkpoint(checkpoint_path, checkpoint)

        topics: dict[str, dict[str, str]] = checkpoint.setdefault("topics", {})
        for agent_id in ordered_agents:
            agent = config.agents[agent_id]
            if agent_id in topics:
                continue
            summary = (
                f"工作区 `{config.workspace.id}` 已初始化。你的稳定角色 ID 是 `{agent_id}`。"
                "请确认收到配置，之后只按状态图协议行动。"
            )
            if agent_id == "organizer":
                summary += (
                    f"\n\n配置摘要：\n```yaml\n{config.to_yaml()}```\n\n```mermaid\n{diagram}```"
                )
            root_id, session_id = await self.admin.create_topic(
                app_id=apps[agent_id],
                chat_id=chat_id,
                title=f"{agent.display_name} · {config.workspace.id}",
                instruction=summary,
                idempotency_key=f"ge:{config.workspace.id}:v{config.workspace.version}:topic:{agent_id}",
            )
            topics[agent_id] = {"root_message_id": root_id, "session_id": session_id}
            self._save_checkpoint(checkpoint_path, checkpoint)

        provisioning = WorkspaceProvisioning(
            workspace_id=config.workspace.id,
            role_profile_id=profile_id,
            chat_id=chat_id,
            bindings=[
                SessionBinding(
                    agent_id=agent_id,
                    lark_app_id=apps[agent_id],
                    chat_id=chat_id,
                    root_message_id=topics[agent_id]["root_message_id"],
                    session_id=topics[agent_id]["session_id"],
                )
                for agent_id in ordered_agents
            ],
        )
        if self.storage is not None:
            await self.storage.save_provisioning(provisioning)
        checkpoint["completed"] = True
        checkpoint["provisioning"] = provisioning.model_dump(mode="json")
        self._save_checkpoint(checkpoint_path, checkpoint)
        return provisioning

    @staticmethod
    def _load_checkpoint(path: Path, config: WorkspaceConfig) -> dict[str, Any]:
        if not path.exists():
            return {"workspace_id": config.workspace.id, "config_hash": config.content_hash}
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid provisioning checkpoint: {exc}") from exc
        if checkpoint.get("config_hash") != config.content_hash:
            raise ConfigError("provisioning checkpoint belongs to a different frozen configuration")
        return checkpoint

    @staticmethod
    def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(checkpoint, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
