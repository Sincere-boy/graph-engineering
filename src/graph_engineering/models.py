from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=100)
    config_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    state_id: str = Field(min_length=1, max_length=100)
    message: str = Field(default="", max_length=100_000)
    created_at: datetime = Field(default_factory=utc_now)
    causation_id: str | None = Field(default=None, max_length=200)


class RouteDecision(BaseModel):
    action: Literal["activate", "pause", "complete", "close"]
    active_node: str | None
    target_agent: str | None = None
    event_ids: list[str]
    state_id: str


class WorkspaceRuntime(BaseModel):
    workspace_id: str
    config_version: int = Field(ge=1)
    config_hash: str
    status: Literal["registered", "running", "paused", "closed", "completed", "unhealthy"]
    cursor: int = Field(default=0, ge=0)
    event_log_identity: str | None = None
    active_node: str | None = None
    health: str = "unknown"
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class Delivery(BaseModel):
    delivery_id: str
    workspace_id: str
    event_ids: list[str]
    target_agent: str | None = None
    status: Literal["pending", "delivered", "failed", "needs_reconcile"]
    kind: Literal["workflow", "recovery"] = "workflow"
    source_cursor: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=1)
    message_id: str | None = None
    reconciliation_source: Literal["organizer_receipt", "operator_evidence"] | None = None
    detail: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SessionBinding(BaseModel):
    agent_id: str
    lark_app_id: str
    chat_id: str
    root_message_id: str
    session_id: str
    session_scope: Literal["group", "topic"] = "topic"


class WorkspaceProvisioning(BaseModel):
    workspace_id: str
    role_profile_id: str
    chat_id: str
    bindings: list[SessionBinding]
    owner_open_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
