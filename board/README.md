# 中文只读 Issue 看板

Symphony 仓库中的中文 GitHub Issue 看板：泳道、运行历史、Pi 实时日志与静态归档。仅依赖 Python 3 标准库。

**看板不再提供审批、标签变更或其他 GitHub 写操作。** 人工直接在 GitHub Issue 上操作 `agent` / `merge-approved`。
完整协议和部署前置条件见 [`elixir/docs/github-workflow.md`](../elixir/docs/github-workflow.md)。

## 源码与数据

```text
board/server.py                 # HTTP 服务，内嵌 HTML / CSS / JavaScript
board/session.py                # 会话查看和静态导出工具
board/tests/test_server.py      # 离线测试
../deploy/symphony-runs.service # 当前服务器部署模板
```

源码位于仓库；工作流、凭据、JSONL 和 HTML 归档不提交 Git。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SYMPHONY_WORKFLOW` | `/root/symphony/config/WORKFLOW.md` | 读取双引号包裹的 `repo: "owner/repo"` |
| `SYMPHONY_ENV` | `/root/symphony/env` | 读取 `GITHUB_TOKEN=...` 行，用于 GitHub 只读请求 |
| `SYMPHONY_SESSIONS` | `/root/symphony/sessions` | Pi 原生 JSONL 归档及 `html/` 静态导出 |
| `SYMPHONY_SESSIONS_LIVE` | `/root/symphony/sessions-live` | Pi RPC 日志镜像，与 `runtime.live_log_dir` 对齐 |
| `SYMPHONY_STATE_URL` | `http://127.0.0.1:8320/api/v1/state` | Symphony 状态 API |

旧的 `SYMPHONY_BOARD_TOKEN` 不再使用；旧 `/api/approve`、`/api/label` POST 一律返回 410。
旧 `?token=...` 地址中的令牌会被移除，不再保存，浏览器中的旧 `sb-token` 也会清除。

会话目录由现有归档流程填充，看板不会自动复制 Pi 全局会话。
扫描时仍保留原有的归档清理行为：清理超过 30 天、已经找到对应原生归档的实时镜像，请保留数据备份。
“只读”指看板不对 GitHub 发起业务写操作，不代表完全不维护本地归档。

## 启动

从仓库根目录：

```bash
python3 board/server.py --bind 127.0.0.1 --port 8322
python3 board/session.py issues
python3 board/session.py issue GH-89
python3 board/session.py export --out /path/to/sessions/html
```

GitHub 不可用时，首页退化为本地会话列表。

## systemd

`deploy/symphony-runs.service` 为当前 `/opt/symphony-src`、`192.168.1.199` 部署模板，其他机器需调整路径和绑定地址。

```bash
sudo install -m 644 deploy/symphony-runs.service /etc/systemd/system/symphony-runs.service
sudo systemctl daemon-reload
sudo systemctl enable --now symphony-runs.service
sudo systemctl restart symphony-runs.service
```

服务直接运行 `/opt/symphony-src/board/server.py`。
当前服务器的旧 `/root/symphony/bin/session-serve`、`session` 是指向仓库文件的兼容符号链接，原脚本有 `.pre-repo-<时间戳>` 备份。
修改源码后需重启看板服务；不需要重启 Symphony 主服务。

- `8320`：Phoenix 原生 Dashboard 与状态 API。
- `8322`：本目录中文只读看板。

## 安全

HTTP 读取接口没有内置认证，日志可能含敏感内容。只供可信环境使用；远程访问应加认证代理和 TLS，勿直接暴露公网。
建议给看板使用独立的只读 GitHub 凭据，不再复用可修改仓库的 Token。

## 测试

```bash
python3 -m unittest discover -s board/tests -v
python3 -m py_compile board/server.py board/session.py
```

测试使用模拟数据和临时本地端口，不读取部署凭据，不请求真实 GitHub。
覆盖空泳道类名隔离、动态切换一致性、状态优先级、首页和 API，以及旧写接口永久停用。
