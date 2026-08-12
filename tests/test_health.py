from graph_engineering.health import evaluate_workspace_health
from graph_engineering.models import SessionBinding


def bindings() -> list[SessionBinding]:
    return [
        SessionBinding(
            agent_id="a",
            lark_app_id="cli_a",
            chat_id="oc_group",
            root_message_id="om_a",
            session_id="session-a",
        )
    ]


def test_health_requires_exact_registered_session() -> None:
    result = evaluate_workspace_health(
        bindings(),
        {"service": {"status": "online"}},
        [{"sessionId": "different", "status": "working"}],
        expected_repository="/repo",
    )

    assert result.status == "degraded"
    assert "missing" in result.reasons[0]


def test_health_surfaces_attention_and_workdir_drift() -> None:
    result = evaluate_workspace_health(
        bindings(),
        {"service": {"status": "online"}},
        [
            {
                "sessionId": "session-a",
                "status": "working",
                "workingDir": "/wrong",
                "agentAttention": True,
            }
        ],
        expected_repository="/repo",
    )

    assert result.status == "needs_attention"
    assert any("working directory" in reason for reason in result.reasons)


def test_health_is_healthy_when_daemon_and_sessions_match() -> None:
    result = evaluate_workspace_health(
        bindings(),
        {"service": {"status": "online"}},
        [{"sessionId": "session-a", "status": "ready", "workingDir": "/repo"}],
        expected_repository="/repo",
    )

    assert result.status == "healthy"
