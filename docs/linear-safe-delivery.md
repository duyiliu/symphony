# Linear 安全自动交付

## 目标

Linear 仍是唯一任务源。宿主负责在 PR 完成后核验真实 HEAD SHA、CI、Review、
分支保护与风险边界；只有确定性门禁全部通过才合并。Pi 只负责实现和提交，
不持有合并能力。

## 门禁顺序

1. 从 Linear Issue attachment 找到唯一 GitHub PR；多个 open PR 视为歧义并暂停。
2. 重新读取 PR，确认同仓库、非 Draft、目标分支正确，并取当前 `head.sha`。
3. 读取该 SHA 的 check-runs 与 commit statuses。无检查、pending、失败都不放行。
4. 读取当前 SHA 的 Review：`CHANGES_REQUESTED` 退回 `Rework`；必需批准数不足则等待。
5. 读取 base 分支保护：要求 admin 不能绕过、禁止 force push/删除；必需 checks
   必须在当前 SHA 成功，必需 Review 数必须满足。
6. 检查 PR 文件数、风险路径和 Issue 风险标签；命中即暂停。
7. 检查 `mergeable` / `mergeable_state`；冲突、behind 退回返工，未知状态等待。
8. 宿主先把 Linear 移到 `Merging`，再调用 GitHub merge（固定 `sha`）。
9. 重新读取 PR，确认 `merged == true` 且 `head.sha` 等于受门禁 SHA。
10. 更新 Linear 为 `Done`，评论 PR、SHA、合并方式和门禁结果。

任何 API 失败、状态变化、合并结果无法确认都 fail closed，不重试合并。

## 自动返工

- 失败 checks、`CHANGES_REQUESTED`、冲突或 behind 可以退回 `Rework`。
- 最多 `delivery.max_rework_attempts` 次；同一 SHA 同一原因不重复返工。
- 新 SHA 出现后，旧 Review、旧 checks 和旧合并结论全部失效并重新核验。
- 超限或返工没有产生新 SHA 时暂停在 `Human Review`，评论人工介入原因。

## 凭据边界

- `SYMPHONY_GITHUB_TOKEN` 只存在于宿主进程环境；Pi 启动脚本会 unset GitHub
  token 变量，并使用独立的 `GH_CONFIG_DIR` / `GIT_CONFIG_GLOBAL`。
- Pi 的 `github_api` MCP 工具可在配置仓库内读取评审与检查信息；写操作还必须
  绑定当前 Linear Issue：新建 PR 的 head 分支、已有 PR 的 head 分支以及评论目标
  都必须包含该 Issue identifier，且只能操作同一仓库内的 open PR。
- `PUT /pulls/{n}/merge` 被 allowlist 明确拒绝；worker 只能把 `symphony`
  label 加到自己的 PR，不能创建通用 Issue，也不能修改其他 PR。
- 分支推送使用只能写 Contents 的部署密钥或细粒度 Token；base 分支保护必须
  阻止直接推送和 force push。
- 合并动作只能由 `symphony.delivery` 使用宿主凭据执行。

## 验证清单

- [ ] 低风险测试 PR：CI 全绿、风险路径未命中，宿主自动合并并回写 Done。
- [ ] 高风险测试 PR：命中 auth/DB/CI 路径，宿主暂停且没有 merge 请求。
- [ ] CI 失败：Issue 自动进入 Rework，新 commit 后重新核验。
- [ ] 冲突/behind：进入 Rework，不自动合并。
- [ ] 缺少 Review 或分支保护：等待或暂停，不合并。
- [ ] PR 已合并后服务重启：不重复 merge，补写 Linear Done。
- [ ] 合并响应丢失/API 失败：重新读取 GitHub，无法确认则暂停。
- [ ] 多 PR、无 PR、PR 关闭：歧义/关闭状态安全暂停。
- [ ] 分支保护不可用：`require_branch_protection: true` 时暂停；测试环境仅在
      `allow_without_branch_protection: true` 时显式放行。
