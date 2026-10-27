# 中文 Issue 看板

Symphony 仓库中的中文 GitHub Issue 看板：泳道、运行历史、Pi 实时日志与静态归档。仅依赖 Python 3 标准库。

看板提供 **批准合并** 与 agent 标签的派发 / 停止操作：按钮用服务端 `GITHUB_TOKEN` 直接写 GitHub 标签，
与人工在 Issue 上操作等价；`agent` / `merge-approved` 互斥，加其中一个会先摘掉另一个。
审批入口仍是 GitHub 标签事件，宿主的交付 / CI / head 校验不变。
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
| `SYMPHONY_ENV` | `/root/symphony/env` | 读取 `GITHUB_TOKEN=...` 行，用于 GitHub 请求（含看板写操作） |
| `SYMPHONY_SESSIONS` | `/root/symphony/sessions` | Pi 原生 JSONL 归档及 `html/` 静态导出 |
| `SYMPHONY_SESSIONS_LIVE` | `/root/symphony/sessions-live` | Pi RPC 日志镜像，与 `runtime.live_log_dir` 对齐 |
| `SYMPHONY_STATE_URL` | `http://127.0.0.1:8320/api/v1/state` | Symphony 状态 API |

旧的 `SYMPHONY_BOARD_TOKEN` 不再使用。`/api/approve`、`/api/label` POST 改用服务端
`GITHUB_TOKEN` 执行标签 / 评论写入，并做同源（Origin / Sec-Fetch-Site）与受信任 issue 校验。
旧 `?token=...` 地址中的令牌会被移除，不再保存，浏览器中的旧 `sb-token` 也会清除。
未配置 `SYMPHONY_WORKFLOW` / `SYMPHONY_ENV` 时操作区会提示“写操作未启用”，写接口返回 503。

会话目录由现有归档流程填充，看板不会自动复制 Pi 全局会话。
扫描时仍保留原有的归档清理行为：清理超过 30 天、已经找到对应原生归档的实时镜像，请保留数据备份。
“看板可写”仅指上述标签与可选评论：不做合并、关闭 issue 或仓库管理操作，最终合并始终走宿主入口。

## 启动

从仓库根目录：

```bash
python3 board/server.py --bind 127.0.0.1 --port 8322
python3 board/session.py issues
python3 board/session.py issue GH-89
python3 board/session.py export --out /path/to/sessions/html
```

GitHub 不可用时，首页退化为本地会话列表。

## 今日完成与历史记录

工作台内提供“处理中 / 今日已完成 / 历史已完成”三个视图，原页切换，不打开新页面。
今日按北京时间的 Issue 关闭日期筛选；历史按关闭时间倒序，搜索支持编号和标题。

已完成仅包含当前已关闭且 `state_reason` 不是 `not_planned` 的 Issue，排除 PR、
待审核交付、暂停和失败记录，不把会话结束当作任务成功，也不直接宣称 PR 已合并。
点击完成卡片在同页抽屉查看 Issue 内容、完成时间和本地运行日志，不提供重复派发/批准按钮。

`/api/completed` 提供已完成数据，缓存 60 秒；原 `/history` 书签重定向回工作台历史视图。
最多扫描最近更新的 1000 条已关闭条目（含被过滤的 PR），到达上限会提示；
GitHub 读取失败会提示并尽可能展示缓存，不静默当作“无记录”。不包含已重开 Issue 的旧关闭事件。

手机端使用三列视图切换按钮、单列完成卡片、可横滑泳道及全屏详情抽屉，搜索框避免 iOS 输入缩放，
操作按钮至少 44px，内容区适配动态视口和安全区。

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
- `8322`：本目录中文 Issue 看板。

## 安全

HTTP 接口没有内置登录认证，写接口只做同源与受信任 issue 校验，日志可能含敏感内容。
只供可信环境使用；远程访问应加认证代理和 TLS，勿直接暴露公网。
看板写入用服务端 `GITHUB_TOKEN`（需要 issue 标签 / 评论写权限）；宿主要求授权标签事件来自
User 类型操作者，共享同一令牌即视为受信任人工操作，请妥善保管。

## 测试

```bash
python3 -m unittest discover -s board/tests -v
python3 -m py_compile board/server.py board/session.py
```

测试使用模拟数据和临时本地端口，不读取部署凭据，不请求真实 GitHub。
覆盖空泳道类名隔离、动态切换一致性、状态优先级、首页和 API，以及写接口的同源拦截、
输入校验、信任检查与互斥标签切换。
