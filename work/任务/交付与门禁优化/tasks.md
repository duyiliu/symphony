# 交付与门禁优化执行队列

设计：同目录 design.md v1，用户已确认。代码基线：main，679b767c7c7e95ee64c90396a901637d59c25419；开始前 board、python 多个文件已有未提交修改，另有 .qoder/、deploy/WORKFLOW.199.md 未跟踪，均保留。

- T01 in_progress：门禁状态真实展示（AC04）；检查状态为未知时不得误报待批准。
- T02 in_progress：已分离评估和 Agent 派发，基础单测通过；取消、重启、重复评估及端到端仍待验证（AC02、AC03）。
- T03 pending：项目交付前产物校验及 CI 分层、稳定汇总（AC01、AC05）。
- T04 pending：确定性测试、对抗式审查与环境验收。

实现审查：未执行；环境验收：未执行。
