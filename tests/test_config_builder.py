from pathlib import Path

from graph_engineering.config_builder import config_from_markdown


def test_builds_generic_config_from_markdown_tables(tmp_path: Path) -> None:
    markdown = """
## Agents
| id | display_name | prompt | skills |
| --- | --- | --- | --- |
| implementer | 实现者 | 完成功能 | test-driven-development |
| auditor | 审计者 | 检查结果 | |

## States
| id | display_name | allowed_writers | action | target |
| --- | --- | --- | --- | --- |
| ready | 就绪 | organizer | activate | implementer |
| audit | 审计 | implementer | activate | auditor |
| redo | 返工 | auditor | activate | implementer |
| done | 完成 | auditor | complete | |
"""

    config = config_from_markdown(markdown, workspace_id="markdown-flow", repository=tmp_path)

    assert config.agents["implementer"].skills == ["test-driven-development"]
    assert config.states["redo"].action.target == "implementer"
    assert config.agents["organizer"].system_managed is True
