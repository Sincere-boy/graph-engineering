---
name: setup-graph-engineering
description: 从已克隆的仓库初始化并验收完整的 Graph Engineering 开发环境。当用户要求安装、初始化、修复或检查 graph-engineering 时使用，覆盖仓库托管的 graph-engineering Codex skill、Node.js 与 Botmux、Python 3.12 Conda 环境、Docker Compose MongoDB、systemd 用户服务和 HTTP 健康检查。
---

# 配置 Graph Engineering 环境

将已克隆的 Graph Engineering 仓库配置为可工作的本地环境，并以当前 clone 为唯一事实源。安装日常使用的 `graph-engineering` skill，但绝不把本 setup skill 安装到用户的全局 Codex skill 目录。

## 支持环境

- Linux x86_64 或 aarch64，并支持用户级 systemd。
- Node.js 22 或更高版本，以及 npm。
- 当前用户可直接访问的 Docker Engine 和 Compose v2。
- 安装在 `~/.graph_engineering/miniconda3` 的 Miniconda。
- Botmux `3.13.0`，且已完成飞书配置。

如果主机不满足上述条件，报告不支持的约束并停止，不要自行设计另一套服务拓扑。不要把 Docker、MongoDB、Botmux Dashboard 或 Graph Engineering API 暴露到 localhost 以外的地址。

## 执行流程

1. 使用 `git rev-parse --show-toplevel` 定位 clone。确认仓库包含 `pyproject.toml`、`compose.yaml`、`deploy/graph-engineering.service` 和两个仓库内 skill。
2. 执行仓库内的只读诊断脚本。首次安装前返回非零是正常现象，将所有 `MISSING` 行作为待办清单：

   ```bash
   skills/setup-graph-engineering/scripts/doctor.sh "$(git rev-parse --show-toplevel)"
   ```

3. 只按照各项目的官方安装文档补齐缺失的系统依赖：
   - 安装 Docker Engine 和 Compose v2 插件，确保当前用户直接执行 `docker info` 成功。不要在仓库脚本前添加无人值守的 `sudo`。
   - 安装 Node.js 22 或更高版本，并执行 `node --version` 和 `npm --version` 验证。
   - 为当前用户把 Miniconda 安装到 `~/.graph_engineering/miniconda3`。只从 `https://repo.anaconda.com/miniconda/` 下载安装包，并在执行前用官方归档中的 SHA-256 校验。除非用户明确要求，否则不要修改 Shell 启动文件。

   如果系统包安装需要管理员权限或交互式输入，展示精确且范围受限的命令并等待用户操作；绝不尝试无人值守提权。
4. 当 Botmux 缺失或版本不一致时，安装已验证的固定版本：

   ```bash
   npm install --global botmux@3.13.0
   botmux --version
   ```

   如果 npm 全局目录不可写，改用用户拥有的 npm prefix，并确保其 `bin` 目录已加入 `PATH`；不要执行 `sudo npm`。
5. 如果 Botmux daemon 或 Dashboard 任一项未就绪，只修复缺失部分。仅在尚未完成首次飞书配置时执行 `botmux setup`，否则保留现有配置：

   ```bash
   botmux setup
   botmux start
   botmux autostart enable
   botmux dashboard current
   ```

   `botmux setup` 和飞书网页授权必须由用户完成。暂停并等待用户操作，然后继续。绝不读取、复制、输出或编辑 Botmux 私有配置、Cookie、Token 或凭据文件；不要轮换已有 Dashboard Token。
6. 以软链接方式安装仓库托管的日常 Codex skill。辅助脚本可幂等执行，并会拒绝覆盖已有真实目录：

   ```bash
   skills/setup-graph-engineering/scripts/install-main-skill.sh \
     "$(git rev-parse --show-toplevel)"
   ```

   告知用户：Codex 可能需要开启新会话才能发现 `graph-engineering`。不要把 `setup-graph-engineering` 链接到 `~/.codex/skills`。
7. 安装引擎、创建 Python 3.12 环境、启动固定版本的 MongoDB 容器，并启用用户级服务：

   ```bash
   GE_SOURCE_DIR="$(git rev-parse --show-toplevel)" \
     skills/graph-engineering/scripts/install.sh
   ```

   安装器必须使用当前 clone、Docker Compose v2、检测到的 Botmux 可执行文件和 `systemctl --user`。重复执行时应协调同一批资源，不得创建替代资源。
8. 再次执行诊断。只有脚本退出码为零且输出 `READY graph-engineering environment` 时，安装才算完成：

   ```bash
   skills/setup-graph-engineering/scripts/doctor.sh \
     "$(git rev-parse --show-toplevel)"
   ```

9. 报告 clone 路径、日常 skill 安装路径、Conda 环境、Botmux 版本、MongoDB 容器状态、systemd 服务状态，以及本地控制台地址 `http://127.0.0.1:8765/`。不得包含任何密钥或带认证信息的 Dashboard URL。

## 失败处理

- MongoDB 失败时，检查 `docker compose -f compose.yaml ps` 和容器健康状态，不要删除数据卷。
- 后端失败时，检查 `systemctl --user status graph-engineering.service` 和 `journalctl --user -u graph-engineering.service`；修复第一个根因后重新执行安装器。
- Botmux 失败时，只使用 `botmux status`、`botmux start`、`botmux dashboard current` 等公开 CLI 命令。凭据修复必须在用户在场时通过 `botmux setup` 完成。
- 绝不通过删除已有 Conda 环境、MongoDB 数据卷、Botmux 状态、Codex skill 目录或 systemd unit 来走捷径。

环境配置不授权登记、资源初始化、恢复或调度任何 Graph Engineering workspace。日常 `graph-engineering` skill 中的 Mermaid 审批门禁只在用户后续要求创建图拓扑时生效，不适用于本次纯环境配置流程。
