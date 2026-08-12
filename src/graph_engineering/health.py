from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from graph_engineering.models import SessionBinding


class HealthResult(BaseModel):
    status: Literal["healthy", "needs_attention", "degraded"]
    reasons: list[str]


def evaluate_workspace_health(
    bindings: list[SessionBinding],
    summary: dict,
    sessions: list[dict],
    *,
    expected_repository: str,
) -> HealthResult:
    service_status = str((summary.get("service") or {}).get("status", "")).lower()
    if service_status not in {"online", "ok", "healthy", "running"}:
        return HealthResult(status="degraded", reasons=["botmux dashboard service is offline"])

    by_id = {str(session.get("sessionId")): session for session in sessions}
    degraded: list[str] = []
    attention: list[str] = []
    registered_topic_roots = {binding.root_message_id for binding in bindings}
    workspace_app_ids = {binding.lark_app_id for binding in bindings}
    workspace_chat_ids = {binding.chat_id for binding in bindings}
    inactive_statuses = {
        "closed",
        "stopped",
        "failed",
        "quarantined",
        "offline",
        "isolated",
    }
    for session in sessions:
        status = str(session.get("status", "")).lower()
        if (
            session.get("larkAppId") in workspace_app_ids
            and session.get("chatId") in workspace_chat_ids
            and str(session.get("rootMessageId", "")).startswith("om_")
            and str(session.get("rootMessageId")) not in registered_topic_roots
            and status not in inactive_statuses
            and not session.get("quarantined")
        ):
            session_id = session.get("sessionId")
            attention.append(
                f"unregistered active session {session_id} violates fixed-topic binding"
            )
    for binding in bindings:
        session = by_id.get(binding.session_id)
        if session is None:
            degraded.append(f"missing registered session for agent {binding.agent_id}")
            continue
        status = str(session.get("status", "")).lower()
        if status in inactive_statuses or session.get("quarantined"):
            degraded.append(f"session {binding.session_id} is {status or 'quarantined'}")
        if session.get("workingDir") != expected_repository:
            attention.append(
                f"session {binding.session_id} working directory drifted from {expected_repository}"
            )
        if (
            session.get("agentAttention")
            or session.get("tuiPromptActive")
            or session.get("pendingRepo")
        ):
            attention.append(f"session {binding.session_id} requires attention")
    if degraded:
        return HealthResult(status="degraded", reasons=degraded + attention)
    if attention:
        return HealthResult(status="needs_attention", reasons=attention)
    return HealthResult(status="healthy", reasons=[])
