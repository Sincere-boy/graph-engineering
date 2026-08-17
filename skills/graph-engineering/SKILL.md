---
name: graph-engineering
description: Install, configure, and provision generic declarative engineering state graphs backed by graphctl, botmux, and Feishu. Use when a user asks to create configurable multi-Agent workflows, define deterministic role routing, provision botmux groups/topics, or create a graph-engineering workspace from YAML or Markdown tables.
---

# Graph Engineering

Use `graphctl` as the single implementation surface. This Skill orchestrates installation and creation commands; it must not edit botmux private files or reimplement routing, Event Log, outbox, provisioning, or health logic.

## Workflow

1. Inspect the target repository read-only and preserve its current worktree. Do not create a branch or `git worktree` yet.
2. Before creating or mutating anything, turn the requested Agents, states, transitions, human waits, and terminal outcomes into a proposed Mermaid `flowchart`. Send the Mermaid diagram to the user in the conversation, ask whether it matches their intent, and stop the turn to wait for explicit approval. Do not install the engine, write or convert configuration, register a workspace, provision resources, resume a workspace, or append the initial event before approval. If the user requests a change, send the complete revised Mermaid diagram and wait again. Approval applies only to the exact diagram shown; any later change to its Agents, transitions, routing, human waits, or terminal outcomes requires a new preview and approval.
3. After approval, create an isolated branch and `git worktree` if repository changes are required; do not merge or push it unless explicitly requested.
4. Ensure the engine is installed by running `scripts/install.sh`. It creates the Python 3.12 Conda environment, starts the pinned MongoDB container, and installs the localhost service.
5. Build configuration that exactly implements the approved Mermaid graph:
   - If input is YAML, run `graphctl config validate <path>`.
   - If input is Markdown tables, use `graphctl config from-markdown`; read `references/config-schema.md` for the exact columns.
   - Keep stable IDs separate from display names. Never add scenario-specific roles or states to engine code.
6. Compare the validated configuration with the approved Mermaid graph before registration. If they differ semantically, return to step 2 and obtain approval for a revised diagram; never silently treat validation as user approval.
7. Register the frozen configuration with `graphctl workspace register <yaml>`.
8. Run `graphctl workspace provision <workspace_id>`. This is resumable and reconciles safe settings: on failure, rerun the same command and retain the checkpoint. It creates one group-scoped Session for the organizer without a fixed topic, and exactly one persistent native topic/session for every other `(workspace_id, agent_id)`. It never recreates an existing binding. Do not create replacement bots, groups, topics, or sessions by hand.
9. When provisioning succeeds, report the workspace ID, configuration version/hash, and the created or reused app/chat/root/session IDs returned by the creation commands, then stop. Do not resume the workspace, append an initial event, dispatch to an Agent, or monitor any later status, Event Log, delivery, recovery, or state transition unless the user makes a separate explicit request.

## Non-negotiable rules

- The Mermaid preview and explicit user approval are a hard gate for every creation or topology-changing operation. Silence, a previous approval for another diagram, or the user's original prose request is not approval. Read-only inspection and status queries do not require this gate.
- Creation ends when `graphctl workspace provision <workspace_id>` succeeds. A creation request does not authorize starting a run or tracking the graph after provisioning.
- Routing comes exclusively from the active frozen config. Never let the organizer model select a target.
- Configuration changes require closing the workspace, a higher version, validation, and registration of a new snapshot.
- MongoDB is the default. Use SQLite only when the user explicitly requests it or MongoDB is unavailable and the choice is recorded; set `GE_STORAGE_BACKEND=sqlite` explicitly.
- Provisioning uses botmux Dashboard APIs and the confirmed Feishu web session. Never read or write botmux credential/config formats.
- The organizer uses the workspace group and must not create a fixed topic. Provision exactly one persistent native topic/session for every other `(workspace_id, agent_id)` and never create replacement bots, groups, topics, or sessions by hand.
- The service listens on `127.0.0.1` unless the user explicitly approves a broader bind.

## Commands

```bash
scripts/install.sh
graphctl config validate /absolute/workspace.yaml
graphctl workspace register /absolute/workspace.yaml
graphctl workspace provision <workspace_id>
```

Do not use `scripts/activate-workspace.sh` for creation because it resumes the workspace after provisioning. After the exact Mermaid graph has been shown and explicitly approved, run the validate/register/provision commands separately and stop after provisioning succeeds.
