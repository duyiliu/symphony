# 容器化部署（Python 版 Tracker）

本目录把 Python 版 Symphony tracker 打包成容器运行，用于在任意 Linux 主机（含 CentOS 7 这类老 glibc 系统）上快速复现，不必再手工安装 Python/Node/pi。

已于 2026-11-12 在 214 实测通过完整真实生命周期（issue `duyiliu/symphony-e2e#7`：导入 → 派发 → 真 PR #8 / 三项 check 全绿 → Human Review 停驻 → 人工批准 → squash-merge → Done → issue 自动关闭）。

## 为什么用 node:22-bookworm 作基础镜像

| 需求 | 选择理由 |
|---|---|
| 跑 pi | bookworm 自带 Node 22，不用另装二进制 |
| 跑 Python | bookworm 的 `python3.11` 满足项目 `>= 3.10` 要求 |
| glibc | 容器内自带新 glibc，**规避 CentOS 7 glibc 2.17 的兼容问题**，不再需要 unofficial node 构建 |
| 体积 | 复用已有 `node:22-bookworm` 镜像层，构建快 |

## 文件说明

| 文件 | 说明 |
|---|---|
| `Dockerfile` | 镜像定义：python3 + python3-yaml + pi 1.5.2 + 代码 |
| `docker-compose.yml` | api / board 两个容器，共享 `./data` |
| `WORKFLOW.md` | 容器版工作流（路径 `/data/*`、端口 18330、独立 `server.api_token`） |
| `land/` | land skill（挂载到 `/app/skills/land`） |
| `./data/` | 运行期数据：`tasks`（唯一状态权威）、`workspaces`、`sessions*` |
| `./pi-agent/` | pi 配置（**必须可写**，见下方坑） |

## 使用

```bash
# 1. 准备 pi 配置（从已有主机复制，含 new-api 凭据）
cp -a /root/symphony-e2e/pi-agent ./deploy/e2e-container/pi-agent

# 2. 准备环境变量（不要提交）
cat > deploy/e2e-container/.env <<EOF
GITHUB_TOKEN=$(gh auth token)
SYMPHONY_API_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
EOF
chmod 600 deploy/e2e-container/.env

# 3. 构建并启动
docker compose -f deploy/e2e-container/docker-compose.yml up -d --build

# 4. 查看
docker logs -f symphony-e2e-api
# 看板: http://<host>:18332/
```

## 已验证的坑

1. **pi 配置目录必须可写**：pi 会在配置目录创建 `auth.json.lock`，只读挂载会报
   `EROFS: read-only file system, mkdir '/pi-agent/auth.json.lock'`。因此 `./pi-agent` 用可写挂载，且是**宿主机配置的副本**，不直接挂 `:ro`。
2. **容器内没有 gh CLI**：git 凭据改用 credential store，由 `hooks.after_create` 从 `$GITHUB_TOKEN` 写入 `/root/.git-credentials`（600）。原生部署用 `gh auth git-credential`，两者等价但凭据来源不同。
3. **git 必须走 199 代理**：`http://192.168.1.199:7890`，已写入 hook（该主机对 `github.com:443` 的 git 通道不通）。
4. **board → api 跨容器访问不是 loopback**：API 对非 loopback 要求 Bearer，因此必须设置 `server.api_token` 和 `SYMPHONY_API_TOKEN`（board 用它发 Bearer，而不是拿 GitHub token，保持"board 不持有 GitHub token"的设计）。
5. **看板端口与 API 端口分开映射**：容器内 board 依赖 `SYMPHONY_STATE_URL=http://api:18330/...` 的服务名解析。

## 与原生部署的差异

| 项目 | 原生（systemd） | 容器 |
|---|---|---|
| 进程管理 | systemd | docker compose |
| 数据位置 | `/root/symphony-e2e/*` | `./data/`（volume 挂载） |
| git 凭据 | `gh auth git-credential` | credential store 文件 |
| 代码更新 | 覆盖 `src/` 后重启 | `docker compose up -d --build` |
| glibc 依赖 | 需 glibc-217 Node 构建 | 无（镜像内解决） |

## 已知限制

- `./pi-agent/auth.json` 含模型凭据，不要提交到仓库（`deploy/e2e-container/pi-agent/` 与 `.env` 已在 `.gitignore`）
- 端口 18330/18332 为容器验证用，避免与原生实例的 18320/18322 冲突
