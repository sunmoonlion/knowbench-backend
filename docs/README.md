# Knowbench Backend 文档说明

当前架构入口为仓库根 `README.md` 与 `k8s/docs/project-guide/`；
部署事实须核对实际 bundle 与运行状态。历史迁移/退役证据按
`k8s/sunmoonai/docs/legacy-backlog/verification-index.md` 的固定 Git 版本查询。

`mooc-manus-v4-*`（3 份）与 `adr/`（18 份）是 2026-07 那套智能体运行时的历史设计与交付记录，
已于 2026-09-29 按所有者的决定删除。要查原文，看提交 `0c1a030` 及更早的 Git 历史。
文中出现过的 `research-app`、`research-admin-backend`、`research-web-frontend` 是 Knowbench 改名前的名字。

当前唯一活动领域名为 `knowbench-app`，唯一 Backend 为 `knowbench-backend`。未来如果创建
新的通用 `research-app`，必须从最新模板独立实例化，不复用这里的历史身份、数据库或运行资源。

## 2026-09-29：旧运行时已删除

2026-07 的 LangGraph 智能体运行时（`application/agent`、`domain/agent`、`infrastructure/agent`、
`infrastructure/graph`，以及它的路由、后台任务、检索客户端、探针脚本和测试）按所有者的决定删除。
规则见 k8s 仓 `tree-build/SDD/architecture/engineering.md`「工程结构」。

| 还留着的 | 原因 |
| --- | --- |
| 数据库里它的表 | 已发布的迁移不删不改；以后用新的迁移去掉 |
| `alembic/versions/` 里它的迁移文件 | 同上 |
| `scripts/pair_fixture.py`、`scripts/delivery_runtime_probe.py` | 模板带来的，要在模板里处理 |

当前任务方法计划的执行视图、成果绑定与取消并发边界见 [方法执行说明](method-execution.md)。

现在的产品代码是工作台：`app/application/workbench/`、`app/domain/workbench/`、`app/infrastructure/workbench/`。

