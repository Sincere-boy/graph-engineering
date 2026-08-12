from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import WorkspaceConfig
from graph_engineering.engine import AuthorizationError, StateGraphEngine, aggregate_consecutive
from graph_engineering.models import Event


def make_event(event_id: str, actor: str, state: str, causation_id: str | None = None) -> Event:
    return Event(
        event_id=event_id,
        workspace_id="arbitrary-flow",
        config_version=1,
        actor_id=actor,
        state_id=state,
        message=event_id,
        causation_id=causation_id,
    )


def test_engine_routes_using_config_not_role_constants(tmp_path: Path) -> None:
    engine = StateGraphEngine(WorkspaceConfig.model_validate(valid_config(tmp_path)))

    decision = engine.decide([make_event("e1", "maker", "inspect")])

    assert decision.action == "activate"
    assert decision.target_agent == "checker"
    assert decision.active_node == "checker"


def test_engine_enforces_allowed_writer(tmp_path: Path) -> None:
    engine = StateGraphEngine(WorkspaceConfig.model_validate(valid_config(tmp_path)))

    with pytest.raises(AuthorizationError, match="not allowed"):
        engine.decide([make_event("e1", "checker", "begin")])


def test_only_organizer_can_write_engine_managed_closed_event(tmp_path: Path) -> None:
    engine = StateGraphEngine(WorkspaceConfig.model_validate(valid_config(tmp_path)))

    decision = engine.decide(
        [make_event("close-1", "organizer", "closed")],
        expected_active_node="checker",
    )

    assert decision.action == "close"
    assert decision.active_node is None
    assert decision.target_agent == "organizer"
    with pytest.raises(AuthorizationError, match="only organizer"):
        engine.decide([make_event("close-2", "maker", "closed")])


def test_consecutive_aggregation_preserves_causal_order() -> None:
    events = [
        make_event("1", "maker", "inspect"),
        make_event("2", "maker", "inspect"),
        make_event("3", "checker", "redo"),
        make_event("4", "maker", "inspect"),
    ]

    batches = aggregate_consecutive(events)

    assert [[e.event_id for e in batch] for batch in batches] == [["1", "2"], ["3"], ["4"]]


def test_human_control_edge_returns_to_original_writer(tmp_path: Path) -> None:
    engine = StateGraphEngine(WorkspaceConfig.model_validate(valid_config(tmp_path)))
    request = make_event("human-1", "maker", "human_required")
    resolve = make_event("human-2", "organizer", "human_resolved", causation_id="human-1")

    pending = engine.decide([request])
    resumed = engine.decide([resolve], event_lookup={"human-1": request})

    assert pending.active_node == "human"
    assert pending.target_agent == "organizer"
    assert resumed.active_node == "maker"
    assert resumed.target_agent == "maker"


def test_human_resolved_requires_valid_human_request(tmp_path: Path) -> None:
    engine = StateGraphEngine(WorkspaceConfig.model_validate(valid_config(tmp_path)))

    with pytest.raises(ValueError, match="causation"):
        engine.decide([make_event("e", "organizer", "human_resolved")], event_lookup={})
