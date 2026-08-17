---
name: setup-graph-engineering
description: Bootstrap and verify a complete Graph Engineering development environment from a cloned repository. Use when a user asks to install, initialize, repair, or validate graph-engineering, including the repository-managed graph-engineering Codex skill, Node.js and Botmux, Python 3.12 Conda environment, Docker Compose MongoDB, systemd user service, and HTTP health checks.
---

# Setup Graph Engineering

Configure a cloned Graph Engineering repository into a working local installation. Treat the clone as the source of truth. Install the repository's `graph-engineering` skill for normal use, but never install this setup skill itself into the user's global Codex skill directory.

## Supported target

- Linux x86_64 or aarch64 with user systemd.
- Node.js 22 or newer and npm.
- Docker Engine with Compose v2, reachable by the current user.
- Miniconda installed at `~/.graph_engineering/miniconda3`.
- Botmux `3.14.0` with a completed Feishu setup.

If the host does not match this target, report the unsupported constraint and stop instead of inventing another service topology. Do not expose Docker, MongoDB, Botmux Dashboard, or the Graph Engineering API beyond localhost.

## Workflow

1. Resolve the clone with `git rev-parse --show-toplevel`. Verify it contains `pyproject.toml`, `compose.yaml`, `deploy/graph-engineering.service`, and both repository skills.
2. Run the repository-scoped doctor. A nonzero result is expected before installation; use its `MISSING` lines as the work list:

   ```bash
   skills/setup-graph-engineering/scripts/doctor.sh "$(git rev-parse --show-toplevel)"
   ```

3. Install missing system prerequisites using their official installation documentation:
   - Install Docker Engine and the Compose v2 plugin. Make `docker info` succeed as the current user; do not prefix repository scripts with unattended `sudo`.
   - Install Node.js 22 or newer and verify `node --version` plus `npm --version`.
   - Install Miniconda for the current user at `~/.graph_engineering/miniconda3`. Download only from `https://repo.anaconda.com/miniconda/` and verify the installer SHA-256 against the official archive before execution. Do not modify shell startup files unless the user requests it.
   If a system package operation requires administrator privileges or an interactive prompt, show the exact scoped command and wait for the user; never attempt unattended privilege escalation.
4. Install Botmux at the pinned compatible version when it is absent or different:

   ```bash
   npm install --global botmux@3.14.0
   botmux --version
   ```

   If npm's global prefix is not writable, configure a user-owned npm prefix and ensure its `bin` directory is on `PATH`; do not use `sudo npm`.
5. If either the Botmux daemon or Dashboard readiness is missing, repair only the missing part. Run `botmux setup` only when initial Feishu configuration is absent; otherwise retain the existing configuration:

   ```bash
   botmux setup
   botmux start
   botmux autostart enable
   botmux dashboard current
   ```

   `botmux setup` and Feishu web authentication require the user. Pause for that interaction, then continue. Never read, copy, print, or edit Botmux private configuration, cookies, tokens, or credential files. Do not rotate an existing Dashboard token.
6. Install the normal repository-managed Codex skill as a symlink. The helper is idempotent and refuses to overwrite an existing real directory:

   ```bash
   skills/setup-graph-engineering/scripts/install-main-skill.sh \
     "$(git rev-parse --show-toplevel)"
   ```

   Tell the user a new Codex session may be required before `graph-engineering` is discovered. Do not link `setup-graph-engineering` into `~/.codex/skills`.
7. Install the engine, create the Python 3.12 environment, start the pinned MongoDB container, and enable the user service:

   ```bash
   GE_SOURCE_DIR="$(git rev-parse --show-toplevel)" \
     skills/graph-engineering/scripts/install.sh
   ```

   The installer must use the current clone, Docker Compose v2, the detected Botmux executable, and `systemctl --user`. Rerunning it must reconcile the same resources rather than create replacements.
8. Run the doctor again. Installation is complete only when it exits zero and prints `READY graph-engineering environment`:

   ```bash
   skills/setup-graph-engineering/scripts/doctor.sh \
     "$(git rev-parse --show-toplevel)"
   ```

9. Report the clone path, installed main-skill path, Conda environment, Botmux version, MongoDB container status, systemd service status, and the local console URL `http://127.0.0.1:8765/`. Never include secrets or the authenticated Dashboard URL.

## Failure handling

- For MongoDB failures, inspect `docker compose -f compose.yaml ps` and the container health without deleting its volume.
- For backend failures, inspect `systemctl --user status graph-engineering.service` and `journalctl --user -u graph-engineering.service`; fix the first causal error and rerun the installer.
- For Botmux failures, use only public CLI commands such as `botmux status`, `botmux start`, and `botmux dashboard current`. Leave credential repair to `botmux setup` with the user present.
- Never delete an existing Conda environment, MongoDB volume, Botmux state, Codex skill directory, or systemd unit as a repair shortcut.

Environment setup does not authorize registering, provisioning, resuming, or dispatching a Graph Engineering workspace. The Mermaid approval gate in the normal `graph-engineering` skill applies later when the user requests a graph topology; it does not apply to this environment-only workflow.
