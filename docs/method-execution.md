# 方法计划的执行视图与开步绑定

这是 WP-10 单元 3 的执行基础与规划服务：为现有 Advisor/Runner 准备唯一执行视图、
事务开步入口及有界规划轮次。规划服务复用现有 TurnDriver 接口，仍未由 Advisor 的真实
调度入口调用；当前真实 v3 任务准入保持关闭。Advisor 成功/失败收尾、工具调用入口、页面
及最终交付还没有整体切换；不能据此声明动态专家已可使用。无新表、迁移或作者契约版本。

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
旧结果恢复成有效结果。确切 pin 已失效时明确拒绝，后续交互/重规划处置仍待接入，不能
临时把 pin 改为 latest。`final_artifacts` 指向计划指定的全部最终实例，不由“最后一轮”猜。

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

接下来在保持 v3 准入关闭的前提下，将规划 turn 接现有沙箱执行体，并将 Advisor、
Runner、MCP 鉴权、ExpertDesk/文件/测试报告统一接此视图。covers 已接固定输入读取；尤其不能
只改步骤栏，或让验收重新取“最新上游”而忽略 Attempt 已绑定的输入。成功交付必须核齐
计划指定的全部最终成果；返工后的失效与人工处置、恢复、取消计费均需真实事务测试后
才能开放准入。发布分支由 qwen 单独维护，本开发不进入正在发布的合并候选。

接线时尤其注意：

- 规划轮次、定数据轮次与方法实例要分开识别；初次方法的范围检查不能被规划 Attempt
  已完成这一事实跳过。新的初次规划派发只携带 parent_version=0。
- 先核实际调用身份再判工具子集。现有 MCP 只凭用户/沙箱令牌，args.task 并非受信调用身份；
  conversation、market、NO_SCOPE 等路径也必须覆盖。不能改整个沙箱的全局配置来冒充
  单个任务的权限，否则可能误伤同用户其他会话。
- 开步、验收、成功输出/游标提交与最终交付使用同一计划及输入绑定。取消与结算同一锁序；
  模型轮次与远程原文/文件读取不要放在持 Task 锁的长事务内。
- UI 要区分已验收历史与当前有效成果，最终交付核全 final_outputs；仍拒绝 v3，直到这些
  调用方及恢复、返工、取消的整链测试通过。
