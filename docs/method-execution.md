# 方法计划的执行视图与开步绑定

这是 WP-10 单元 3 的执行基础、规划服务与 Advisor 执行循环。Advisor 已接入有界规划、
实例派发、固定输入验收及完整最终成果交付，复用现有 Runner/TurnDriver 与 Task/Attempt。
工具调用入口、运行页面与测试成果读取已接线；作者页面联调和真实沙箱验收尚未完成，
真实 v3 任务准入保持关闭，不能据此声明动态专家已可使用。本执行段无新表或迁移。

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

读取面已接入 ExpertDesk、文件定位、审批定位、测试成果读取及页面。无计划只显示作者
身份；有计划显示实例、选择理由、计划调整历史与当前成果版本。底稿只使用有效前缀及
全部最终成果，历史结果仍保留供核对。测试产出按下节的可选多成果回执交回。

MCP 调用鉴权已经接线，见下节。v3 仍拒绝外部准入；尚需与作者方法库页面、孵化测试页
以及真实沙箱共同验收，不能将替身协议测试当作上线许可。固定专家
响应不增加 plan:null，原非空 knowbase 数据 HTTP 契约快照必须逐字通过。

发布分支由 qwen 单独维护，本开发不进入正在发布的合并候选。

## 作者测试的稳定期望与多份最终成果

沿用 knowlab 提供的 `knowbot-test-run/v1` 和原有 `equals/approx/contains` 判据。
动态清单的 `expectations[].artifact` 必须是作者在 `planning.final_units` 声明的
方法别名，例如 `report`；不能填任务运行后才生成的 `report_42` 或内部 Artifact 名。
作者端在建测试/签数据授权之前拒绝不匹配的期望，运行方也做相同语义检查。

`final_results` 经当前接受计划的 `final_outputs` 取得确切实例，再按既有已验成果规则
读取 ID/版本/摘要。计划校验已要求最终实例是该方法在计划中的最后一次调用；测试读取
仍核收据，不自行按位置或同名最新 Artifact 猜测。返工后的失效结果不能满足断言。

提供方仅扩展 case-outputs schema：动态回执带 `plan_version` 和 `final_outputs`。
每份保留方法别名、实例 ID、已验成果引用和受大小限制的 JSON；未完成的引用为 null。
有登记文件时通过原有作者工作区读取通道附该文件，读取失败或超时有说明；不再仅取
最后一份文件。成果引用摘要是已验 JSON 的摘要，不能冒充后来读取的文件文字摘要。
步骤的 `current=false` 表示历史尝试仍可查、现已不构成有效成果，不能只看 `accepted`。

每份 JSON/文字和总响应沿用既有上限；超限先去内容并记省略，不去掉最终成果身份。
旧固定专家不发这些可选字段，原单文件路径与响应形状保留。请求、结果及回答 schema
没有新增字段，provider 与消费方只更新产出 schema 摘要，不另造断言引擎或接口版本。
测试：`test_method_test_outputs.py`、`test_method_test_exports_db.py`、既有契约锁及测试
运行回归；真实数据库覆盖多实例、多文件、未验同名新版、返工失效及未规划任务。


## 真实轮次与工具权限

`method_tools.py` 复用现有 Task/Attempt、沙箱身份与会话事件。Runner 发 turn/start 前捕获
活动 Attempt，收到响应后将实际 turn_id 写入 refs.runtime_turn_id；它不等于计费完成，
不会提前写入用于费用去重的 turn_ids。绑定失败要中断并保留实际用量，不能收到响应却
丢掉费用。规划使用宿主只读策略、不挂用户执行环境，不准提权；方法轮次恢复原执行环境。

HTTP MCP 在所有本地工具和数据中继之前检查：

- `_meta.x-codex-turn-metadata` 的线程、轮次与 callId 必须与 Runner 经已认证 app-server
  收到并落库的 item/started 相符，server、tool、完整 arguments 也一致。metadata 本身
  不是凭据，模型给的 task/conversation 更不是。沙箱 JWT、线程所有者及沙箱记录必须对应。
- HTTP 与 websocket 可能先后到达，最多等 2 秒让事件/轮次绑定落库；不锁 Task 等网络，
  超时拒绝，不回退放行。turn/completed 后旧调用不能重放。
- 动态任务只认当前活动 Attempt 及其已接受计划、固定输入和所选工具。规划轮次无工具；
  定数据只许 find_subject/describe_items/request_data；方法按冻结子集。拒绝借另一任务、
  conversation 或 scope=market 绕过；lookup/describe_items 即使不带 task，也必须通过
  该轮次的工具子集检查，再走现有 knowbase 公共发现规则。
- DataRelay 仍向 knowbase 传服务端持有的运行授权。模型不能直接提供底层 run/session；
  方法工具授权不替代数据授权，更不替用户确认采单。动态定数据在 MCP 未配置时失败，
  不把“没有工具”写成“数据齐了”。
- 为防去掉 task 参数或取消后退回旧入口，账户一旦存在动态任务历史，该账户的调用都需
  可核实的执行来源。普通聊天使用原有 turn/accepted（by=user）证明，仍可并行使用自己的
  数据会话；仅有固定专家的账户保持既有路径。不能手工删除历史来规避此检查。

原生审批按活动方法和实际 turn_id 定位，规划/定数据不能借下一方法的权限放行；绑定
损坏、旧轮次或作者明确 deny 都拒绝，已有任务授权和重复请求不能扩大作者边界。该检查
针对 app-server 发来的审批请求；本轮没有另建本机命令执行器或声称屏蔽所有只读宿主工具。
`tools/list` 仍列平台能力，调用时强制限制，不能将清单展示当作每轮的权限凭据。

### 固定版本协议探针

`scripts/probes/codex_mcp_provenance.py` 只在无外网、全新 home 的一次性容器中运行；Responses
和 MCP 都是 loopback 合成服务，不读任何用户 Key、不调用真实模型/Wind。以项目固定的
Codex 0.155.1 验证：模型 MCP 调用有线程/轮次/callId，且与 item/started 一致；客户端直接
mcpServer/tool/call 没有轮次身份。thread/start 的 MCP header 可分线程，但已装载线程的
thread/resume 不更新 header，unsubscribe 后再 resume 才更新。因此实现不依靠热重写全局
header，不动其他线程配置。升级 Codex 时须重新跑此探针，不能把版本实测当永久协议保证。

运行方式（准备本地已验证的固定版本二进制及已有含 Python 的镜像；不自动下载）：

```sh
probe_codex_bin=/absolute/path/to/pinned/codex
probe_python_image=your-existing-python-image
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 0 --tmpfs /tmp --tmpfs /root \
  -v "$probe_codex_bin:/probe/codex:ro" \
  -v "$PWD/scripts/probes/codex_mcp_provenance.py:/probe/probe.py:ro" \
  --entrypoint python "$probe_python_image" /probe/probe.py
```

探针打印版本与五个核验布尔值；不打印凭据、请求头或用户消息。通用接口参考
[官方 app-server 文档](https://developers.openai.com/codex/app-server/)与
[MCP 配置文档](https://developers.openai.com/codex/mcp/)，上面的热更新与 metadata 结论
以固定二进制实测为依据。生产接入前仍需真实沙箱的完整工具轮次验收。

`test_method_tools_db.py` 覆盖伪造元信息/参数、旧轮次、跨沙箱/任务/会话、市场与无范围
入口、取消后省略 task、绑定损坏、并发到达、底层 grant 注入、原生审批与作者 deny。
`test_method_runner_mcp_db.py` 经真实 Runner websocket 读取、真实 MCP HTTP 路由和隔离
PostgreSQL，调用合成 knowbase 并完成计划/方法/最终交付；它仍不代表真实供应商验收。

规划上下文另带最多 100 份已钉资料元信息（名字/来源/原件版本/读法）与 100 条请求编号和状态，超出显式计数；不带 grant、未读取正文或未确认需求内容。它只帮助选择方法，不能作为数据已可读的授权证明。

### 补采并发提交边界

knowbase 的 `resolution_changed` 表示解析期间基版已变，工作台返回明确的
提采错误，要求重读后重试，不伪装为 `listed`。本地收到正常结果后按 Task → resolution
顺序锁定、重读最新 detail，再追加请求；保留远程调用期间写入的预切合等字段，拒绝
重复编号和终态任务的迟到回包。作者测试没有远程清单，编号与追加在同一短事务完成。
取数和远程解析不放进这些行锁事务，既有需要续跑任务的收尾使用同一锁序。

提供方 knowbase 在既有修订锁下校验读取基版，过期追加/修改/刷新回 409，不覆盖赢家。
旧提供方即便同时报成功，本地重复编号也会报错而非覆盖另一请求；但这不能修复旧提供方
已丢失的远程清单，联合验收必须用带基版检查的 knowbase。

尚未覆盖远程追加成功但 HTTP 响应丢失、或本地写入前进程退出的自动恢复；重试可能收到
`need_key_taken`，不能当作成功，也不能绕过本人确认直接取数。当前改动没有引入操作
收据/自动对账，不声称跨服务 exactly-once。用户确认、改采单与新请求同时发生的完整
并发合并也仍需单独验证；此段只保证所列提采提交与 provider 基版冲突的边界。
