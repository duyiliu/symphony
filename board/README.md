# 中文 Issue 看板

Symphony 仓库中的中文 GitHub Issue 看板：泳道、运行历史、Pi 实时日志与静态归档。仅依赖 Python 3 标准库。
主项目之外的 GitHub / GitLab 项目可按地址导入，以只读方式查看 open issue 与已完成历史。

看板提供标签派发 / 停止 / 人工介入操作：按钮用服务端 `GITHUB_TOKEN` 直接写 GitHub 标签，
与人工在 Issue 上操作等价；`agent` / `needs-human` 互斥，加其中一个会先摘掉另一个。

- `agent` 是唯一执行开关：开发、交付后由宿主自动校验检查/风险规则并完成合并，无需人工逐 PR 授权。
- 运行中的 Issue 可“转人工处理”（加 `needs-human` 并摘 `agent`）；已暂停的 Issue 显示暂停原因
  （取自宿主暂停评论），处理后“恢复执行”（移除 `needs-human`、重新加 `agent`）。
- 旧的 `merge-approved` 已从协议移除：批准入口返回 410，遗留标签不影响执行。
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
| `SYMPHONY_PROJECTS` | `<WORKFLOW 所在目录>/projects.json` | 导入项目注册表，只存元数据（含 Token 变量名），不含 token |

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

## 多项目与导入

“主项目”仍由 `WORKFLOW.md` 的 `repo:` 决定，泳道、运行记录、标签操作行为不变；
其他项目通过“导入项目”加入，以只读方式展示 open issue 泳道与已完成历史。

- 注册表：`SYMPHONY_PROJECTS` 指定 JSON 文件，默认 `<WORKFLOW 所在目录>/projects.json`。
  文件只保存项目元数据（地址、平台、显示名、Token 环境变量名），**不保存 token 本身**；
  写入用临时文件 + 原子替换，权限 `0600`。文件缺失或损坏时忽略并只打印一次日志，主项目不受影响。
- 支持类型：`github`（github.com 与 GitHub Enterprise）和 `gitlab`
  （gitlab.com、自建 GitLab、子组项目）。平台一般自动识别；自建实例需在导入弹窗显式选择平台。
  地址支持 `https://host/path`、`git@host:path.git`、`ssh://git@host/path.git` 和裸 `host/path`；
  仅允许 https（本机 `127.0.0.1` / `localhost` 可用 http 调试）。
- 凭据：按项目的 `token_env` 读取（默认 GitHub `GITHUB_TOKEN`、GitLab `GITLAB_TOKEN`，
  GitLab 未配置时依次回退 `GITLAB_PAT`、`GITLAB_ACCESS_TOKEN`）。值取自服务进程环境或
  `SYMPHONY_ENV` 文件里的 `KEY=...` 行，例如：

  ```text
  GITHUB_TOKEN=ghp_xxx
  GITLAB_TOKEN=glpat-xxx
  ```

  无 token 时匿名读取公开项目；私有项目导入会返回明确错误（401/403/404 提示配置 token）。
- 只读语义：导入项目的卡片是外链（新窗口打开 issue 页面），不参与本地运行记录 / 日志 / 抽屉，
  也不提供 `agent` / `needs-human` 等写操作；编号带项目级前缀（导入 GitHub 为 `GHX-<n>`，
  GitLab 为 `GL-<iid>`），不会与主项目的 `GH-<n>` 会话记录混淆。
- 缓存与上限：每个项目独立缓存 60 秒；open issue 最多 3 页 / 300 条，已完成最多 10 页 / 1000 条，
  到达上限会提示；读取失败时保留上次缓存并给出警告。
- 移除：仅导入项目可移除，主项目由 WORKFLOW 配置，接口返回 400。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 主项目 + 导入项目；只返回 `token_env` / `token_configured`，不含 token |
| POST | `/api/projects` | body `{url, provider?, name?, token_env?}`，导入成功后返回 201 |
| DELETE | `/api/projects/<urlencoded id>` | 删除导入项目；`primary` 返回 400 |
| GET | `/api/issues?project=<id>` | 默认 `primary`；未知项目返回 404 |
| GET | `/api/completed?project=<id>` | 默认 `primary`；未知项目返回 404 |
| GET | `/?project=<id>` | 服务端直出选中项目；未知 id 回退主项目 |

### 扩展新项目类型

`board/server.py` 中的 `ProjectProvider` 是项目类型适配器：实现 `api_base` / `headers` /
`project_ref` / `valid_path` / `normalize` / `fetch_open` / `fetch_closed`（可选覆盖编号前缀），
注册进 `PROVIDERS`，再给导入弹窗的 `#pm-provider` 加一个选项即可；
路由、注册表和只读看板渲染不需要改动。

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
导入项目会让服务端带着所选 `token_env` 的令牌访问用户填写的 host（禁止跨主机重定向），
因此导入同样只应在可信环境、可信局域网内使用。

## 测试

```bash
python3 -m unittest discover -s board/tests -v
python3 -m py_compile board/server.py board/session.py
```

测试使用模拟数据和临时本地端口，不读取部署凭据，不请求真实 GitHub。
覆盖空泳道类名隔离、动态切换一致性、状态优先级、首页和 API，以及写接口的同源拦截、
输入校验、信任检查与互斥标签切换、人工介入端点与旧批准入口下线；
另覆盖项目地址解析、注册表读写（只存元数据）、项目级缓存隔离、导入 / 删除接口与只读页面渲染。
