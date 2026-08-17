from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


def _executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_environment_installer_uses_clone_and_detected_tools(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".graph_engineering"
    command_log = tmp_path / "commands.log"
    conda = _executable(
        root / "miniconda3/bin/conda",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'conda %s\\n' "$*" >> {command_log}
if [[ "$*" == "env list" ]]; then
  printf 'graph-engineering /fake/env\\n'
fi
""",
    )
    _executable(root / "miniconda3/envs/graph-engineering/bin/graphctl", "#!/bin/sh\n")
    fake_bin = tmp_path / "bin"
    _executable(
        fake_bin / "docker",
        f"#!/bin/sh\nprintf 'docker %s\\n' \"$*\" >> {command_log}\n",
    )
    _executable(
        fake_bin / "systemctl",
        f"#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> {command_log}\n",
    )
    botmux = _executable(fake_bin / "botmux", "#!/bin/sh\n")
    installer = REPOSITORY / "skills/graph-engineering/scripts/install.sh"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GE_ROOT_DIR": str(root),
        "GE_SOURCE_DIR": str(REPOSITORY),
        "GE_CONDA_BIN": str(conda),
    }

    result = subprocess.run(
        [str(installer)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text(encoding="utf-8")
    assert f"pip install -e {REPOSITORY}[dev]" in commands
    assert f"docker compose -f {REPOSITORY}/compose.yaml up -d --wait mongodb" in commands
    override = home / ".config/systemd/user/graph-engineering.service.d/override.conf"
    assert f"Environment=GE_BOTMUX_CLI={botmux}" in override.read_text(encoding="utf-8")
    assert (home / ".local/bin/graphctl").resolve() == (
        root / "miniconda3/envs/graph-engineering/bin/graphctl"
    )


def test_environment_installer_refuses_to_overwrite_graphctl_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".graph_engineering"
    conda = _executable(
        root / "miniconda3/bin/conda",
        """#!/usr/bin/env bash
if [[ "$*" == "env list" ]]; then
  printf 'graph-engineering /fake/env\\n'
fi
""",
    )
    _executable(root / "miniconda3/envs/graph-engineering/bin/graphctl", "#!/bin/sh\n")
    target = _executable(home / ".local/bin/graphctl", "#!/bin/sh\necho mine\n")
    fake_bin = tmp_path / "bin"
    _executable(fake_bin / "docker", "#!/bin/sh\n")
    _executable(fake_bin / "systemctl", "#!/bin/sh\n")
    _executable(fake_bin / "botmux", "#!/bin/sh\n")

    result = subprocess.run(
        [str(REPOSITORY / "skills/graph-engineering/scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GE_ROOT_DIR": str(root),
            "GE_SOURCE_DIR": str(REPOSITORY),
            "GE_CONDA_BIN": str(conda),
        },
    )

    assert result.returncode != 0
    assert "refusing to overwrite existing graphctl" in result.stderr
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_main_skill_installer_links_repository_skill_idempotently(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    installer = (
        REPOSITORY
        / "skills/setup-graph-engineering/scripts/install-main-skill.sh"
    )
    env = {**os.environ, "CODEX_HOME": str(codex_home)}

    first = subprocess.run(
        [str(installer), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    second = subprocess.run(
        [str(installer), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    installed = codex_home / "skills/graph-engineering"
    assert installed.is_symlink()
    assert installed.resolve() == REPOSITORY / "skills/graph-engineering"


def test_main_skill_installer_refuses_to_overwrite_a_real_directory(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    installed = codex_home / "skills/graph-engineering"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("local changes", encoding="utf-8")
    installer = (
        REPOSITORY
        / "skills/setup-graph-engineering/scripts/install-main-skill.sh"
    )

    result = subprocess.run(
        [str(installer), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == "local changes"


def test_doctor_reports_missing_user_installation_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    doctor = REPOSITORY / "skills/setup-graph-engineering/scripts/doctor.sh"
    home = tmp_path / "empty-home"
    home.mkdir()

    result = subprocess.run(
        [str(doctor), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "GE_ROOT_DIR": str(home / ".graph_engineering"),
        },
    )

    assert result.returncode == 1
    assert "MISSING codex-skill" in result.stdout
    assert "MISSING conda" in result.stdout
    assert "dashboard-token" not in result.stdout


def test_doctor_rejects_node_older_than_22(tmp_path: Path) -> None:
    doctor = REPOSITORY / "skills/setup-graph-engineering/scripts/doctor.sh"
    fake_bin = tmp_path / "bin"
    _executable(fake_bin / "node", "#!/bin/sh\necho v20.19.0\n")
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [str(doctor), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CODEX_HOME": str(home / ".codex"),
            "GE_ROOT_DIR": str(home / ".graph_engineering"),
        },
    )

    assert result.returncode == 1
    assert "MISSING node Node.js 22 or newer" in result.stdout


def test_doctor_rejects_incompatible_botmux_version(tmp_path: Path) -> None:
    doctor = REPOSITORY / "skills/setup-graph-engineering/scripts/doctor.sh"
    fake_bin = tmp_path / "bin"
    _executable(
        fake_bin / "botmux",
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 3.12.0; fi\n",
    )
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [str(doctor), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CODEX_HOME": str(home / ".codex"),
            "GE_ROOT_DIR": str(home / ".graph_engineering"),
        },
    )

    assert result.returncode == 1
    assert "MISSING botmux-version expected 3.13.0, found 3.12.0" in result.stdout


def test_doctor_requires_complete_repository_layout(tmp_path: Path) -> None:
    doctor = REPOSITORY / "skills/setup-graph-engineering/scripts/doctor.sh"
    incomplete = tmp_path / "incomplete-clone"
    (incomplete / "skills/graph-engineering").mkdir(parents=True)
    (incomplete / "pyproject.toml").write_text("", encoding="utf-8")
    (incomplete / "skills/graph-engineering/SKILL.md").write_text("", encoding="utf-8")

    result = subprocess.run(
        [str(doctor), str(incomplete)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )

    assert result.returncode == 1
    assert "MISSING repository expected complete graph-engineering clone" in result.stdout


def test_doctor_requires_python_312_in_conda_environment(tmp_path: Path) -> None:
    doctor = REPOSITORY / "skills/setup-graph-engineering/scripts/doctor.sh"
    home = tmp_path / "home"
    root = home / ".graph_engineering"
    conda = _executable(
        root / "miniconda3/bin/conda",
        """#!/usr/bin/env bash
if [[ "$*" == "env list" ]]; then
  printf 'graph-engineering /fake/env\\n'
fi
""",
    )
    _executable(
        root / "miniconda3/envs/graph-engineering/bin/python",
        "#!/bin/sh\nprintf 'Python 3.11.9\\n'\n",
    )

    result = subprocess.run(
        [str(doctor), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "GE_ROOT_DIR": str(root),
            "GE_CONDA_BIN": str(conda),
        },
    )

    assert result.returncode == 1
    assert "MISSING python-version expected Python 3.12, found Python 3.11.9" in result.stdout
