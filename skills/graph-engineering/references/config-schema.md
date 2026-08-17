# Workspace schema

YAML requires `schema_version: 1`, an absolute `workspace.repository`, arbitrary `agents`, and arbitrary `states`. The engine injects `organizer`; a user may provide organizer display name, prompt additions, or skills but cannot remove its protocol.

```yaml
schema_version: 1
workspace:
  id: stable-workspace-id
  name: Optional display name
  version: 1
  repository: /absolute/worktree
agents:
  worker_id:
    display_name: Visible name
    prompt: Role-specific instructions
    skills: []
states:
  state_id:
    display_name: Visible state
    allowed_writers: [organizer, worker_id]
    action:
      type: activate
      target: worker_id
```

Each state has exactly one action: `activate` with one target or `complete`. Cycles and multiple writers are valid. At least one `complete` state is required.

Provisioning maps the injected organizer to one group-scoped Botmux Session without a fixed topic.
Every declared non-organizer Agent maps to exactly one persistent Botmux native topic/session for
each workspace. State transitions never create topic resources.

The IDs and display names `human_required`, `human_resolved`, `待人工`, and `人工已处理` are reserved and must not appear in `states`.

For Markdown import, use these two tables (English or Chinese section headings `Agents/角色` and `States/状态`):

```markdown
## Agents
| id | display_name | prompt | skills |
| --- | --- | --- | --- |
| worker | 实现者 | 完成任务 | skill-a, skill-b |

## States
| id | display_name | allowed_writers | action | target |
| --- | --- | --- | --- | --- |
| ready | 待处理 | organizer | activate | worker |
| done | 完成 | worker | complete | |
```

Then run:

```bash
graphctl config from-markdown input.md --output workspace.yaml \
  --workspace-id stable-id --repository /absolute/worktree
```
