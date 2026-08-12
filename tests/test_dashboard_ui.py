import json
import subprocess
from pathlib import Path


def test_dashboard_render_plan_only_updates_changed_sections() -> None:
    module = (
        Path(__file__).parents[1]
        / "src"
        / "graph_engineering"
        / "static"
        / "dashboard_state.js"
    )
    script = f"""
      import {{ changedSections }} from {json.dumps(module.as_uri())};
      const previous = {{
        workspaces: [{{ workspace_id: "ws", status: "running" }}],
        graph: {{ nodes: [{{ id: "agent", active: true }}], edges: [] }},
        sessions: [{{ session_id: "s1", status: "idle" }}],
        activity: [{{ actor: "agent", message: "done" }}],
      }};
      const unchanged = structuredClone(previous);
      const sessionChanged = structuredClone(previous);
      sessionChanged.sessions[0].status = "working";
      const activityChanged = structuredClone(previous);
      activityChanged.activity.push({{ actor: "reviewer", message: "approved" }});
      if (changedSections(previous, unchanged).length !== 0) process.exit(1);
      if (JSON.stringify(changedSections(previous, sessionChanged)) !== '["sessions"]') {{
        process.exit(2);
      }}
      if (JSON.stringify(changedSections(previous, activityChanged)) !== '["activity"]') {{
        process.exit(3);
      }}
    """

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
