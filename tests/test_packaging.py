from pathlib import Path


def test_user_service_uses_systemd_home_specifier() -> None:
    repository = Path(__file__).resolve().parents[1]
    unit = (repository / "deploy" / "graph-engineering.service").read_text()

    assert "/home/" not in unit
    assert "%h/.graph_engineering/miniconda3/envs/graph-engineering/bin/graphctl" in unit
    assert "--control-dir %h/.graph_engineering" in unit
