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
    for binding in bindings:
        session = by_id.get(binding.session_id)
        if session is None:
            degraded.append(f"missing registered session for agent {binding.agent_id}")
            continue
        status = str(session.get("status", "")).lower()
        if status in {"closed", "stopped", "failed", "quarantined"} or session.get("quarantined"):
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
