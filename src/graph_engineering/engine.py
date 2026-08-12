from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_engineering.config import WorkspaceConfig
from graph_engineering.models import Event, RouteDecision


class AuthorizationError(ValueError):
    pass


def aggregate_consecutive(events: Sequence[Event]) -> list[list[Event]]:
    batches: list[list[Event]] = []
    for event in events:
        if batches and batches[-1][0].state_id == event.state_id:
            batches[-1].append(event)
        else:
            batches.append([event])
    return batches


class StateGraphEngine:
    def __init__(self, config: WorkspaceConfig):
        self.config = config

    def decide(
        self,
        events: Sequence[Event],
        *,
        event_lookup: Mapping[str, Event] | None = None,
        expected_active_node: str | None = None,
    ) -> RouteDecision:
        if not events:
            raise ValueError("events are required")
        state_id = events[0].state_id
        if any(event.state_id != state_id for event in events):
            raise ValueError("a decision batch must contain one consecutive state")
        for event in events:
            self._validate_identity(event)
        self._validate_turn(events, expected_active_node)

        if state_id == "human_required":
            if any(event.actor_id == "organizer" for event in events):
                raise AuthorizationError("organizer cannot request the engine-managed human edge")
            return RouteDecision(
                action="activate",
                active_node="human",
                target_agent="organizer",
                event_ids=[event.event_id for event in events],
                state_id=state_id,
            )
        if state_id == "human_resolved":
            if any(event.actor_id != "organizer" for event in events):
                raise AuthorizationError("only organizer may resolve a human edge")
            cause_id = events[-1].causation_id
            if not cause_id:
                raise ValueError("human_resolved requires causation_id")
            original = (event_lookup or {}).get(cause_id)
            if original is None or original.state_id != "human_required":
                raise ValueError("causation_id must reference a human_required event")
            self._validate_identity(original)
            return RouteDecision(
                action="activate",
                active_node=original.actor_id,
                target_agent=original.actor_id,
                event_ids=[event.event_id for event in events],
                state_id=state_id,
            )

        state = self.config.states.get(state_id)
        if state is None:
            raise ValueError(f"unknown state: {state_id}")
        for event in events:
            if event.actor_id not in state.allowed_writers:
                raise AuthorizationError(
                    f"actor {event.actor_id} is not allowed to write state {state_id}"
                )
        action = state.action.type
        target = state.action.target if action == "activate" else None
        active_node = target if action == "activate" else None
        return RouteDecision(
            action=action,
            active_node=active_node,
            target_agent=target,
            event_ids=[event.event_id for event in events],
            state_id=state_id,
        )

    @staticmethod
    def _validate_turn(events: Sequence[Event], active_node: str | None) -> None:
        if active_node is None:
            return
        if active_node == "human":
            if all(
                event.actor_id == "organizer" and event.state_id == "human_resolved"
                for event in events
            ):
                return
            raise AuthorizationError("only organizer may resolve the active human node")
        unexpected = sorted({event.actor_id for event in events if event.actor_id != active_node})
        if unexpected:
            raise AuthorizationError(
                f"active agent is {active_node}; out-of-turn writers: {unexpected}"
            )

    def _validate_identity(self, event: Event) -> None:
        if event.workspace_id != self.config.workspace.id:
            raise ValueError("event workspace does not match configuration")
        if event.config_version != self.config.workspace.version:
            raise ValueError("event configuration version does not match active version")
        if event.actor_id not in self.config.agents:
            raise AuthorizationError(f"unknown actor: {event.actor_id}")
