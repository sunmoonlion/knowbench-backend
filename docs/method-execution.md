# 方法计划的执行视图与开步绑定

这是 WP-10 单元 3 的执行基础、规划服务与 Advisor 执行循环。Advisor 已接入有界规划、
实例派发、固定输入验收及完整最终成果交付，复用现有 Runner/TurnDriver 与 Task/Attempt。
工具调用入口、页面与测试报告尚未整体接通，真实 v3 任务准入保持关闭；不能据此声明
动态专家已可使用。无新表、迁移或作者契约版本。

## 运行数据与职责

| 入口 | 职责 |
| --- | --- |
| `domain/workbench/execution_plan.py` | 将冻结目录和已接受计划投影成现有 StepContract；只解析实例名、输入连接、covers/back_to 和工具子集，不重编译作者方法 |
| `application/workbench/plan_receipts.py` | 共用的只读收据入口；按 Task 指针读确切计划版本，核任务/作者摘要/规划轮次归属 |
| `application/workbench/execution_plan.py` | 任务局部读取、已验收成果及输入版本核验、当前 Attempt 权限绑定检查 |
| `Ledger.open_method_step` | 持 Task 锁核状态/版本/方向盘/取消，再绑定输入、按作者预留预算并开启现有 Attempt |
| `Ledger.accept_method_plan` | 与派发共用已验收成果读取规则，重规划不能从任意最新 Artifact 推断成果已通过 |
| `application/workbench/method_planner.py` | 用现有 TurnDriver 发普通规划轮次；严格解析提案，经 Ledger 接受，预算、取消和失败均入现有账 |
| `active_method_inputs` / `judge_numbers` | 动态 covers 按活动 Attempt 绑定的输入版本验收；不读同名最新成果 |

同一作者包的多个任务不共用计划。固定专家仍读原有包；动态投影不写入作者包缓存，也不
覆盖 `pack_snapshot` 或 `pack_digest`。未知运行契约、无已接受计划、越权工具均拒绝。

Attempt 的 `step_id` 是本任务的实例 ID；`refs` 记录计划版本/摘要、作者包摘要、方法别名/
版本、实例位置和所选工具。`input_artifact_versions` 保存槽、上游实例、成果名、版本、ID
和摘要。输入中的 future binding 在开步时绑定，已经写明版本的 binding 不会自行跟随新值。
计划快照、作者方法与执行成果分别保留版本，不增加第二套运行状态或计费账。

`accepted_results` 只认游标前的已完成实例，该实例最近一次执行 Attempt 必须成功，再按它登记的输出
ID/版本取原件；不按名称取最新 Artifact。每一项还要核其历史计划、方法身份、工具范围与
输入链。回退游标立即排除后缀；上游返工后，即使错误地推进游标，也不能把依赖旧版本的
旧结果恢复成有效结果。确切 pin 失效时，开步事务先拒绝，Advisor 再发有界规划轮次；
只允许将失效后缀的显式输入版本重新绑定到同一上游已验收新版，形成新计划收据。
已完成前缀、已开始实例的方法/顺序/依赖/权限及旧收据不改，不能临时把 pin 改为 latest。`final_artifacts` 指向计划指定的全部最终实例，不由“最后一轮”猜。

## 并发与取消

Task/Attempt 的写路径统一先锁 Task，再锁 Attempt。`attempt_transition` 只无锁读取
不可变 task_id 来找 Task，状态仍在加锁后核对；取消在 Task 锁内读取当前活动 Attempt。
此前取消先窥读活动 ID，再锁 Task，会漏掉其间新开的轮次，造成任务已取消但预留仍在。
本轮已补确定性并发复现：旧实现残留 0.05，修复后轮次取消且预留归零。开步、取消、
无人推进收尾和迟到费用使用同一顺序，避免一条路径反向加锁。

## 验证与后续接入

规划服务只把作者规划原文、方法选择条件/输入槽/输出形状/允许工具、既有计划及有效成果
版本交给模型。输出采用既有 MethodPlan 严格契约，不能夹带正文、验收或预算；过长的问题
按作者 input_max_chars 截断并显式标记。作者的输入槽和任务实例分开，重规划提示与账房
均保护已开始前缀；失败轮次也消耗作者限定的规划次数。并发调用只发一轮；费用超限不接受
计划；环境断开、超时、中断的进度文字不能当作有效计划。

`run(task_id, parent_version=0)` 是初次规划；调整必须显式携带当前父计划版本。已有计划
时重试初次派发、或重试已经落库的调整均只读返回，不能自动升级为新一轮重规划并收费。

规划结果结算持 Task 锁：完成 Attempt、费用、计划 Artifact/指针/事件、回到 QUEUED 一起
提交。非法提案先整体回滚，再单独结算失败费用并留拒绝原因，下一轮能据此修正；取消后
的迟到响应只补记费用，不接受提案。turn_id 在终态结算时与费用一起登记，避免只登记
RUNNING 的 turn_id 就被误认为已付费。故障回滚后仍由现有 Runner 收尾/恢复，不另建账。

`turn.py` 仅抽出原 Advisor 的 TurnResult/TurnDriver/时限，Advisor 保留旧 TurnResult 导入
入口；没有新增模型客户端。测试中的 Driver 是替身，不能当作真实沙箱规划或生产集成验收。

运行 `tests/test_execution_plan.py`、`tests/test_execution_plan_db.py`、
`tests/test_method_plan.py`、`tests/test_method_plans_db.py`、`tests/test_workbench_ledger_db.py`。
数据库测试使用既有 `AGENT_TEST_DATABASE_URL` 的隔离 schema，包含并发开步、取消竞态、
预算拒绝、事件故障回滚、重复方法、跨任务隔离、重启、旧成果失效及固定专家回归。
合成测试不调用模型、外部证券数据、Wind 或线上任务。

规划与固定输入验收另跑 `tests/test_method_planner_db.py`、
`tests/test_method_acceptance_db.py`、`tests/test_knowbot_compute.py`；覆盖提案修正/重启、
非法工具、已执行前缀改写拒绝、并发规划、预算不足/超限、取消迟到计费去重、跨任务误用、
事件故障整体回滚以及“同名新版本不替换开步输入”的 covers 验收。

## Advisor 执行与原子收尾

Advisor 在无计划时调用 MethodPlanner，随后按任务视图开方法 Attempt；规划和定数据的
完成不算作首个方法已执行，范围检查仍在首个方法进行。每个步骤边界先检查数据等待，
最后一步也不能绕过未确认的补采单直接成功。

验收的远程原文/文件读取不持 Task 锁；结果结算再次核活动 Attempt、计划和绑定。
输出 Artifact、费用/turn_id、成功或失败事件、游标与回队列/返工/交人同一事务提交。
验收服务异常或绑定不符不得收成果，已发生的费用仍结算为平台失败。取消期间返回的
结果仅补费用；旧回执不能撤销新轮次的问题，也不能释放新轮次占用的项目。

最终交付再次检查全部有效前缀及 final_outputs，只用验收收据指向的成果，排除返工旧版。
文件指纹读取后复核 Task 版本/取消；handback、产出 Outbox 与成功终态同事务。
故障回滚后可从已完成游标恢复交付，不再次调用模型。动态 terminal_result_ref 指向包含
全部最终成果引用的 handback，固定专家仍保留原最终成果引用语义。

新增 `tests/test_method_advisor_db.py` 覆盖完整规划/执行/返工/重启/多最终成果、明确钉版
失效后的新计划与次数耗尽、事件故障回滚、验收异常计费、取消期间验收/文件校验、迟到
回执与新轮次交错。另用既有 ScriptedAppServer 驱动真实 Runner 的 WebSocket 协议循环；
这是预录响应工程测试，不是付费真实模型或真实渠道验收。

## 后续接入边界

在保持 v3 准入关闭的前提下继续 Runner 审批定位、MCP 鉴权、ExpertDesk/文件/测试报告
及页面，统一使用有效视图。MCP 当前只绑用户/沙箱，args.task 并非受信调用身份；
conversation、market、NO_SCOPE 等路径也必须覆盖。不能改整个沙箱的全局工具配置来冒充
单个任务的权限，也不能只改提示词或步骤栏就开放动态执行。

发布分支由 qwen 单独维护，本开发不进入正在发布的合并候选。
