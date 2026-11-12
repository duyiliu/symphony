# Symphony Tracker 生产部署（214）

本目录是 214 主机（CentOS 7）上 Tracker 看板生产实例的完整部署物料与操作手册。
214 作为生产环境于 2026-11-12 上线，已通过完整真实端到端验收（issue `duyiliu/symphony-e2e#4`：导入 → 派发 → 真 PR/CI → Human Review → Rework 全量重启 → Merging → squash-merge → Done）。

## 拓扑

| 组件 | 说明 |
|---|---|
| `symphony-e2e-api.service` | 调度器 + HTTP API + MCP bridge，端口 `18320` |
| `symphony-e2e-board.service` | 看板 UI，端口 `18322`，依赖 api |
| `/root/symphony-e2e` | 数据与运行时根目录 |
| `/root/symphony-e2e/tasks` | 任务记录（**唯一状态权威**，每 30 分钟 cron 备份） |
| `/root/symphony-e2e/workspaces` | worker 工作区（终态自动清理） |
| `/root/symphony-e2e/sessions{,-live}` | pi 会话与实时日志 |
| `/root/symphony-e2e/backups` | `tasks-*.tar.gz`，保留最近 30 份 |

## 运行时依赖（214 特殊点，均隔离在 /root/symphony-e2e）

1. **Python 3.12.16**：`cpython312/`（python-build-standalone 免编译包，scp 自本机）
2. **Node 22.22.0**：`node22/` —— CentOS 7 的 glibc 是 2.17，**官方 Node 二进制不可用**（需 glibc≥2.28），必须用 Node 官方 unofficial-builds 的 **glibc-217 构建**：
   `https://unofficial-builds.nodejs.org/download/release/v22.22.0/node-v22.22.0-linux-x64-glibc-217.tar.xz`
   （sha256 `db4a1d582e6fffcf7fb348149ca4ac8fa685699c5bc46cd7e22bbf9a7e673454`）。npmmirror 无此变体。
3. **pi 1.5.2**：`pihome/`（npm 本地安装，不用全局）
4. **pi 配置**：`pi-agent/`（auth.json/models.json/settings.json），通过 `PI_CODING_AGENT_DIR` 指向，与主机其它 pi 配置隔离

## 网络（214 特有）

- `api.github.com` 可直连；但 **`github.com:443` 的 git 通道不通**（clone 报 `Encountered end of file`）
- git 必须走 **199 的 HTTP 代理**：`http://192.168.1.199:7890`
- 已写入 `WORKFLOW.md` 的 `hooks.after_create` 与 `run_api.sh` 的全局 git 配置
- 模型走 `new-api/deepseek-v4.1-flash`（199:3002），214 可直连

## 凭据

- GitHub：214 本机 `gh` 已登录 `duyiliu`（repo/workflow scope），`run_api.sh` 启动时 `gh auth token` 注入内存；worker 环境仍剥离 token（设计内）
- API 鉴权：非 loopback 访问 `/api/v1/*` 需 Bearer；看板页面本身无登录鉴权（局域网使用）

## 操作手册

```bash
# 状态
systemctl status symphony-e2e-api symphony-e2e-board
# 日志
journalctl -u symphony-e2e-api -f
journalctl -u symphony-e2e-board -f
# 重启（代码更新后）
systemctl restart symphony-e2e-api symphony-e2e-board
# 备份（手动）
/usr/local/sbin/symphony-backup.sh
```

- 开机自启：`systemctl enable symphony-e2e-api symphony-e2e-board`（已执行）
- 崩溃自愈：两服务均 `Restart=always`（已执行）
- 任务备份：cron `*/30 * * * *`，保留 30 份（已执行）

## 代码更新流程

1. 本机更新 `python/symphony`、`board/` 后，重新打包上传：
   ```bash
   tar -czf symphony-src.tar.gz python/symphony python/requirements.txt board
   scp symphony-src.tar.gz root@192.168.1.214:/root/symphony-e2e/
   ssh root@192.168.1.214 "cd /root/symphony-e2e && tar -xzf symphony-src.tar.gz -C src && rm symphony-src.tar.gz && systemctl restart symphony-e2e-api symphony-e2e-board"
   ```
2. 若 WORKFLOW/凭据/端口变化，同步本目录物料后按上面重启。

## 已知限制

- 看板无登录鉴权（按需可后续加反向代理/内网隔离）
- worker 终态 workspace 清理发生在调度器启动时（非即时）
- `task_api` 清空 `pr_number` 需用 `null`（0 会被拒；`workpad_comment_id` 又接受 0，校验不一致，见 tasks.md 顺延项）