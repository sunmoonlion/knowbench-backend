# 2026-11-02：专家数据工具执行前检查（#57 修复候选）

真实任务 9319ae09 只读核查：最终 FAILED（14:46:36 UTC，用户停止），前七步按 json_object/required_keys/no_positioning_advice 放行；成稿因 JSON 不合格返工等待。不是后台记成功，但数据不可读未在步骤前阻断。集群 knowbase-service-access 仍无 /api/mcp/data；入口声明由 qwen 按已有发布授权补齐，勿在本工位改线上对象。

新 runtime_tools 使用现有 AppServerConnection，从服务端冻结步骤读取允许的数据工具，执行前以当前 threadId 查询真实 mcpServerStatus/list；缺项/发现异常明确平台失败，无 turn/start、无 token 消耗、不落成果、不推进游标。不自动 reload、不代答、不改授权；方法规划与无数据工具步骤不多探测。失败说明不输出供应商正文/凭据。协议从运行中的 Codex 0.155.1 生成 schema 核实（toolsAndAuthOnly/threadId/toolsError）。固定与动态共用同一入口。

初始开发在 b86cdcdb 后隔离工位，最终只交本补丁到 #57 后端 31e37014143cfa2bf95e2ee28e0dd9d759bdf829，不夹带尚未发布的作者准入配置或商城动态增量。初轮 75 passed；补规划失败分类与动态真 Runner/工具回归后 85 passed，ruff/pyright 通过。日志 /tmp/luna-preflight-final-tests.log、/tmp/luna-preflight-pyright.log。旧专家弱验收未被篡改，尚需在新专家版本补作者确认的数值/出处判据；此补丁不宣称能识别所有无证据文字或运行中所有工具错误。

规则：C-C7 只用 app-server 协议；C-D11 状态仍由 Ledger；C-R10 本地测试不等于线上验收。回退恢复 #57 镜像/源码，保留历史任务。无迁移/新配置。下一步交 qwen 同批发布：入口路由到位、工作台目录完整、旧线程刷新、本人再做新任务；不得复活已失败任务或改其产物。

此前记录：

# 2026-11-02 Wind 通用查询与年报证据（开发候选，未发布）

基线 `24b6415aeaa36e4cf9a6dc4ed113232607ac9859`。本轮本人要求第二轨作为主演示、巨潮保底，财务与年报都纳入、不限制为财务专线。

新增 discover_source_tools（owner 从任务身份取，不认模型传 owner），数据准备可按真实 schema 提出 source_query；参数进入既有确认/预切合/交付链。模型不能复用自己提供的 request_id，解析器生成新查询身份。不扩大作者方法冻结工具范围。
验证定向数据/MCP/方法权限 94 passed / 1 failed，旧只读工具清单断言漏新 discovery；修正后该模块 6 passed（/tmp/luna-wind-bench-mcp-final.log），最终请求形状/身份 15 passed（/tmp/luna-wind-bench-last-tests.log）。ruff 全仓通过、pyright 0 errors。未跑本轮后端全量；已有动态测试构造开关不变，正式 v3/商城仍关闭。

发布由 qwen/Jenkins；不代点用户采单、不修改线上任务。以下为此前历史：

# 工具回传故障与步骤推进修复（2026-11-02）

发布基线：Jenkins #43 的 `7a1c4beaa30fd1c66b1ec12d75fd6388109e2450`；分支：`fix/tool-roundtrip-release`。原修复先在 6032cbd 基线验证，再移植到当前线上基线。
范围：knowbench-backend 的轮次结果归属和专家步骤失败判定。未发布、未修改线上任务、专家 bindings 或供数账。

## 已确认的缺陷与修改

- `runner.py` 原来把任一 error 通知传播给所有活跃 turn，且未按 0.155.1 的嵌套 `error.message` 读取。现在匹配 threadId 和 turnId，忽略 willRetry=true 的暂态错误，保留终态错误与中断。
- `advisor.py` 原来只在没有 final_text 时将 error 判为失败。模型的进度文字不能代表成功。普通步骤有错误即失败；定数据还拒绝超时、中断、用户停止产生的半成品需求，不能将 resolution 置为 ready 或推进 cursor。
- 不修改 bindings；空 bindings 的专家仍可通过 find_subject / request_data 提出需求，走确认后的 knowfeed 流程。

## 线上诊断证据（诊断摘要，不是原始日志副本）

环境：sandbox-platform-dev，sandbox-u-0c520909ee72-7dc6b5c68d-bcljx；Codex 0.155.1；Kimi kimi-k3；Responses 接口。

历史业务任务 `3f1546e4-4173-4298-8149-2ff280b28865` 的 find_subject 已返回 600519.SH。rollout 中 function_call 与 function_call_output 均使用 `mcp__sunmoon_workbench--_--find_subject_1_1dac9f6e`；400 却要求 `mcp__sunmoon_workbench--_--find_subject:1`。仅凭这一点不能把改写责任归给 Codex。

在同一个 app-server 中建立 ephemeral 诊断会话，关闭 web_search，禁用业务 MCP 和 shell，只提供无副作用的虚构动态工具。初次两个独立会话均失败：

| thread | Codex 回调 callId | Kimi 400 要求 |
| --- | --- | --- |
| 01a197da-da4c-7f02-ab75-2fdfe560ec15 | probe_lookup_0_c5d444e3 | probe_lookup:0 |
| 01a197da-e882-7612-acc1-8354605dd028 | probe--_--probe_lookup_0_3a3e318d | probe--_--probe_lookup:0 |

这推翻了“关闭内置搜索即可根治”的说法。随后在没有修改全局配置的情况下，新临时会话出现成功：

- `01a197f6-9899-75d0-9208-75dc332cc532`：临时回环诊断中转，记录原始响应与下一轮请求中的 call_id；两边均为 `probe_lookup_0_d0ab8631`，结果 completed。
- `01a197f7-36dd-73b2-b377-0b53ff1a35f4`：透传响应头的对照也 completed，call_id 两边一致。
- `01a197f7-be47-7560-af52-ce0f9ca0931a`：恢复原始直连路径，completed。
- `01a197f8-aa0e-7591-9854-86fb0df6e62a`：要求先发进度文字再调工具，completed，call_id 两边一致。

临时中转仅存在于诊断进程中，结束即退出，未安装为服务或修改正式请求链路。没有输出认证值或完整业务提示。原始会话诊断脚本在 `/tmp/luna_live_roundtrip_probe.py`，抓取字段的临时脚本在 `/tmp/luna_wire_probe_*.py`；这些临时文件不能作为持久交付依赖。

后续已确定触发条件：诊断线程 `01a197fd-c778-7fb2-9e8f-288d54bbf1c8` 中，Moonshot 在两条 function_call 之后返回 `message(role=assistant, content=[{type:output_text,text:""}])`；Codex 将其保留在 function_call_output 前。相同请求原样重放两次均 400；仅删除空消息即 200/completed；把空消息挪到调用前则报另一条 400（text content is empty）。调用 ID 全程一致，无需改写。

对应请求在沙箱 `/tmp/luna-wire-cases/aacc587a-96a8-4890-a796-da7a4334ca16.json`；该文件只有虚构诊断数据、请求体与事件，不包含认证请求头。源代码修复在 sunmoonun 的 `image/moonshot_responses.py`：在用户沙箱回环 HTTP/SSE 出口，仅过滤 Moonshot Responses input 中的无内容助理消息，不改工具调用、工具结果、ID 或搜索声明。正式适配代码重放原始失败请求已得到 200/completed，多个真实 Codex 工具轮次也在记录过滤计数后完成。没有增加第二个模型执行器、隐式重试或新的外部数据目的地。

联网搜索工具声明已确认仍会发往 Moonshot；这些工具探针没有实际产生 webSearch 完成事件，因此不能把它们写成“联网搜索端到端已验收”。完整业务提采闭环仍须部署后的工作台验收。

## 配置问题

qwen/sunmoonun 的未提交 entrypoint 改动曾把 `web_search_mode = "never"` 写入 `[features]`。实际 0.155.1 会报 `invalid type: string "never", expected a boolean in features`。线上实际 config 没有这一项，且 REWRITE_CONFIG=0，因此此前“配置与环境一致且已根治”的说法不成立。

已在获得目录写入权限后仅删除这条无效生成行，保留其余未提交改动。这个改动在 qwen 工作区，不属于本后端分支提交。有效的搜索模式是顶层 `web_search` 的 disabled/cached/live；本次未全局关闭搜索。`standalone_web_search=false` 本身不能证明所有搜索入口关闭，更不能修复此次调用 ID 错误。

## 验证与待办

回归覆盖：错误只影响匹配会话、可重试错误不污染成功、终态错误保留、中断显式、定数据失败/超时/中断即使带进度文字或部分需求也不推进。使用独立数据库 luna_roundtrip_tests；每个测试使用隔离 schema。

命令：`python3 /tmp/luna_roundtrip_db_tests.py tests/test_workbench_turn_outcome.py tests/test_workbench_expert_data_db.py -q --tb=short`；该本地包装器仅从现有测试容器读取连接配置供进程使用，不输出凭据。

已运行 ruff format、ruff check 和 git diff --check。两组回归在原基线和 Jenkins #43 基线上均为 39 passed；沙箱兼容检查 14 passed；确切 Codex 0.155.1 的 config/read 已验证启动覆盖生效且 PVC 配置不变。

剩余：按用户要求通过现有 Jenkins release 发布；当前线上 OCI 为 sha256:1557b13795d88fb041e52475ff98516581229ed23018a6d629f66c479a1d2fa0，修复基于其来源 1104dcd/c00e6766，不把旧工作区声明直接发布。发布前确认部署版本与回退入口；发布后通过实际工作台验证失败不误推进与 find_subject → request_data → 用户确认的闭环；若再次出现 400，保留脱敏的同一次失败请求/响应配对并与模型供应商核对。没有未经确认地派发真实采集任务。

## 2026-11-02 luna：发布 #44 后真实提采验证与契约修复

#44 已发布。用户更新沙箱后的真实任务 b26d4452-8aaa-4671-9c27-dcb77e813791 成功完成 find_subject 与 request_data，定数据轮次 completed，停在 WAITING/DATA；确认与 demand 均为空。八个专家步骤仍完整。原工具回传 400 在该轮未复现；不能因此宣称采集、分析和报告均验收通过。

新发现：工具向模型仅声明任意 doc_type 字符串和 period 对象，实际契约有枚举与范围；描述还把 fixed 错误地限定为非定期材料。真实模型连续猜错季报类型，并以 interim_announcement 替代，年报用 latest 导致范围从用户的 2021–2024 扩大到 2020–2025。等待时 now_view 又显示下一步专家作者的 why，误导为不允许补采。

修复：request_data 的文档类型、时间、主题和数量字段复用现有锁定 DataRequest 契约生成 JSON Schema（含自包含 $defs）；说明明确季度类型、显式日期可用于定期报告、额外基期分开说明、拒绝后查 describe_items，不能随意改材料类型。WAITING/DATA 的 now.step 显示数据准备与核对范围的说明，恢复运行后保留原步骤说明。未修改专家方法、bindings、用户采单或确认状态。

验证：后端全量 1196 passed、92 skipped（独立测试库），ruff 通过。pyright 有 16 条错误，与 #44 基线逐条一致，无新增；不能记为类型检查通过。新增 schema 回归覆盖四种定期报告、fixed 日期保留、错误词汇与边界拒绝、非财务材料兼容；新增等待/恢复说明回归。jsonschema 仅为开发测试依赖。

真实用户沙箱的无副作用动态工具探针 thread 01a19835-b043-7173-98f8-9cc1e2e166ee 使用候选 schema，Kimi 接受 $defs，并提交 annual_report / q1_report / semiannual_report / q3_report，均保留 2021-01-01 至 2024-12-31；轮次 completed，工具往返一次。此探针未调用业务 MCP、未提采，不替代真实专家验收。

待办：与前端长采单布局修复经现有 Jenkins 发布，保留人批阶段。旧任务不自动确认；新任务重验数据范围，再由用户确认是否采集。

## 2026-11-02 第二轨准备：类型检查基线清理

#45 已发布为 release-20261102-2；五工位 130 个检出已对齐并干净后，按所有者要求在 luna 开发。发现并消除原基线 16 条 pyright 错误：应用端口签名明确为已存在的参数（不改 HTTP 契约）；holds_task 标为 TypeGuard；可空 scope/等待原因按类型收窄；仓储 SQL 参数和 CursorResult 明确类型；安装包 object_key 缺失按既有不可用出口处理。不通过 Any 强转整个客户端或忽略错误掩盖端口不一致。

验证：ruff 通过，pyright 0 errors/0 warnings；连同随后 WP-2 分数次幂工作区运行后端全量 1204 passed、92 skipped。独立测试库；原始输出 /tmp/luna-wp2-pyright.txt 与 /tmp/luna-wp2-pytest.txt。本批尚未发布，线上仍 #45。

## WP-2：通用分数次幂（本地，未发布）

审阅 qwen 历史 74a443c 后，在 #45 基线上重新落实其通用算法：整数次幂保持原行为，指数绝对值不超过 100；正底分数次幂用 Decimal ln/exp、44 位中间精度再回到 34 位，零负次幂保持除零出口。同步 MCP 工具说明；不增加 cagr/root/DCF 专用函数，不硬编码公司、期间或财务科目。

测试用可独立计算的合成序列 1000→1331（三年、年增长 10%）验证公式，不把未经核对的公司数值冒充金标准；另覆盖精确平方根、三次根舍入、负分数指数、底数/指数边界、重复逐位一致，以及真实计算记录进入 computed 验收、伪造输出被拒。目标测试 82 passed，全量 1204 passed/92 skipped；ruff、pyright 均通过。0030/0048 说明同步在 luna/sunmoonun，提交待随工作包登记。完整财务金标准与预测方法仍需作者确认，不能由这些合成测试替代。

下一步：依所有者 11-02 新决定设计任务内动态步骤/工具选择，方法论仍来自作者；Wind 当前只准本人研发、首轮最多 5 次真实取数，服务端接入待 W0 许可核验。固定保底题目标季度为 2025Q1/Q2，基期 2024 年度；必须区分报告期与当时可得信息，不把使用后来披露年报的历史演算声称为无前视回测。

## 2026-11-02 WP-10 单元 1：动态作者目录（后续记录）

从 7d8da3e18ef055aa38f821a17e6a7f464bc7795c 新建 feat/dynamic-methods，不推进 qwen 正在使用的 fix/supply-delivery。作者目录模型在 domain/workbench/method_catalog.py，生成父仓 dynamic-methods/v1 schema。catalog_from_manifest 检查 v3、方法引用/循环/必需成果闭包、限额与平台工具权限；目录不提供 workflow/step(index)，避免被固定调度器误当任务计划。pack_from_manifest 当前明确拒绝 v3，直到计划/Attempt/有效视图和最终验收都接通再开放。

孵化 v1 原样携带动态工作稿，新增 planning/selection 建议路径，与 knowlab provider/锁同步。规划原文使用严格 PlanningContract；不能借 planning 注入单元 engineering。三方共同验证的非财务样例是 knowlab 编译器实际输出，保存在 tests/fixtures/dynamic-methods-manifest.json，无真实数据。

完整 PostgreSQL 回归 1244 passed / 92 skipped（/tmp/luna-wp10-bench-full.txt）；全量启动后的最终动态计数边界与孵化契约更新，完整重跑受影响 6 文件 81 passed（/tmp/luna-wp10-bench-final-affected.txt）。最终全仓 ruff/pyright 通过。不能把 1244 当作最后每一行代码都重跑全量；最终差异由 81 项覆盖。无迁移、无生产操作、无 Wind 调用。

下一单元是有界线性任务计划、确定性校验、Artifact 版本与 Task 接受指针/CAS，随后接现有 Attempt 预算、取消/恢复、有效计划视图、输入槽绑定实例和成果版本、必经/最终验收。不要直接把 catalog 转成缓存的 ExpertPack.workflow，不要把可变计划写入 pack_snapshot/digest。所有者要求重新孵化财务专家新版本，作者确认方法库；旧保底巨潮专家继续保留。


## 2026-11-02 WP-10 单元 2：任务计划、事务接受与恢复（本地验证完成，未发布）

基线 cbb6cd40e93a3819cca2fb5839caa1df09d77441，继续 feat/dynamic-methods；生产、Jenkins、sources 与 qwen 发布分支未改。Ledger 增加 open_method_plan / accept_method_plan，application/workbench/method_plans.py 只是账房内部实现，不创建第二个任务状态机或模型客户端。

- domain/workbench/method_plan.py 校验有界线性组合，区分方法目录别名、调用实例、成果版本。工具只缩小作者/平台交集；拒绝缺必需方法、超轮次/步骤/调用上限、前向引用、未验成果、旧版本与冲突成稿路径。新计划保留已开始前缀；模型不得提交方法正文、验收或预算。
- 复用 Attempt 预留/结算与 Artifact 版本；Task 接受指针、计划成果和事件单事务。Task 行锁/CAS 串行化重复回执与取消，历史回执只读重放，不能回退已接受指针或复活任务。规划失败计轮次/实际费用，缺预算不建 Attempt；重启读取确切版本，损坏收据拒绝。
- 迁移 20261102_0035（前版 20261030_0034）仅加可空正整数 accepted_plan_version，并同步迁移文件清单。无固定任务回填；空指针可 downgrade/upgrade，有接受记录拒绝删指针。应用回退保留扩展 schema，不能删业务历史。
- 合成目录、无模型/网络调用；独立 PostgreSQL schema 定向最终 54 passed（/tmp/luna-method-plan-tests-final.txt），包括实际并发接受/取消两种锁顺序、同包两任务、故障回滚、失效成果、收据损坏和迁移保护。首次失败是测试 fixture 两会话复用幂等键，已改按会话生成键，未放松业务校验。初始输出 /tmp/luna-method-plan-tests-first.txt 保留。完整 PostgreSQL 回归 1289 passed / 92 skipped / 2 个既有 collection warnings（/tmp/luna-method-plan-full.txt，420.91 秒）。全量启动后仅补了恢复故障测试与说明性注释，相关三个文件最终另跑 54 项通过；不把该全量数声称为新补四例也在其中。最终 app 目录内 uv run --locked pyright 为 0 errors，ruff check app tests alembic/versions/20261102_0035_method_plans.py 通过；输出 /tmp/luna-method-plan-{pyright-final,ruff-final}.txt。一次在仓根运行 pyright 未使用 app 目录配置，产生 3 个错误，原输出保留于 /tmp/luna-method-plan-pyright-wrong-cwd.txt；正式检查按项目目录重跑通过。

所有者追加确认三层：方法单元独立版本、专家精确引用、任务动态组合。当前目录仍是专家内方法，尚未实现独立主档。详见 sunmoonun 的 methods.md 与 decisions.md；WP-10 新增单元 1a，先补 knowlab 作者主档、输入槽/专家连接分离和跨应用引用契约，再开放运行。计划 method_id 只是冻结目录内的别名，不能用它在运行时挑选全局方法新版。此单元仍拒绝 v3 执行；未接 advisor 派发、有效视图、运行鉴权、返工失效传播、页面和最终验收。接续时不得把任务计划塞入共享 ExpertPack 缓存。


## 2026-11-02 WP-10 单元 3 首段：有效视图、事务开步与取消边界

- 开发基线 707ef08cdb6763ccc1a47f4d5d7d2c9b0bc7ef12，独立 `feat/method-execution-view`。qwen 正在使用的合并候选父仓 `release/recovery-methods@2f08c6999feb03f3005393c81f430513ea197ad7` 没有移动；本轮不发版、不加新发版交接、不改线上任务或采单。Wind 仍 3/5，没有新增调用。
- 已实现 `domain/workbench/execution_plan.py` 纯投影、`plan_receipts.py` 共用读取、`execution_plan.py` 任务输入/成果/权限绑定与 `Ledger.open_method_step` 原子开步。复用现有 StepContract/Attempt/Artifact/预算，不写入作者包缓存、不新增迁移。重规划共用已验收成果读取，修复按名称取 latest 可能误选未验收版本的问题。
- 已完成游标前成果的计划、方法、输入链核验；回退后后缀立即不可用、上游改版后旧依赖拒绝。明确钉住的旧版失效不会擅自跟随 latest；后续返工交互/重规划处置仍待接入。
- 发现原取消逻辑先读活动 ID 再锁 Task，开步恰在中间发生会漏关新 Attempt。以发布候选的 request_cancel 替换测试运行中的方法，确定性复现预留残留 0.05（/tmp/luna-cancel-before-fix.txt）。已将开步、取消、结算、无人收尾及迟到费用统一 Task → Attempt 锁序，并校验迟到费用的任务归属；新回归通过，未绕过或删除断言。
- 完整后端回归 1325 passed / 92 skipped / 2 既有 collection warnings（/tmp/luna-execution-plan-full-final.txt）；全量启动后最后增加跨任务视图误用守卫及一例，五个受影响文件最终重跑 80 passed（/tmp/luna-execution-plan-final-focused.txt），没有将它冒充另一轮全量。最终全仓 ruff、pyright 0 errors（/tmp/luna-execution-plan-{ruff,pyright}-final.txt）。更早的 1324 项全量是取消锁序修复之前的证据，不能替代本次结果。
- 下一段继续接现有规划 turn、Advisor/Runner、工具调用鉴权、covers 的固定输入、页面/文件/测试报告、最终成果原子交付。当前仅提供经过测试的执行基础入口，真实 v3 准入仍拒绝；不能只改步骤栏或直接开放运行。实现在 docs/method-execution.md，根仓 methods.md 与双轨计划同步。回退应用保留已有 0025/0035 历史，不删除计划或成果。

## 2026-11-02 WP-10 单元 3 第二段：有界规划服务与固定输入验收

- 从 `0e0917aff9217be0da98fb853419b7befd734447` 继续 `feat/method-execution-view`。新增 MethodPlanner，复用现有沙箱 TurnDriver 端口、Ledger、Attempt 和计划 Artifact；把原 Advisor 的 TurnResult/TurnDriver/时限抽到 turn.py 共用，没有新增模型客户端或状态机。Advisor 的真实调度入口尚未调用本服务；测试 Driver 是替身，不能称真实沙箱已跑通。
- 模型只收作者规划原文、冻结方法选择条件/输入槽/允许工具及既有计划和有效成果；严格解析 MethodPlan。保护已开始前缀、已完成输入钉确切成果版本，非法工具与改写前缀拒绝。失败提案计轮次与费用，下一轮可看拒绝原因；并发只发一轮。`run(parent_version=0)` 初次派发重试不会自动变成重规划，后续调整须明确父版本。
- 规划 Attempt 完成、费用、计划/指针/事件与回队列在 Task 锁内同事务。非法提案回滚后再结算失败费用；取消迟到只补费用且按 turn_id 去重，超时/中断/环境丢失/超预算不收部分提案，事件故障整体回滚。预留失败不发 turn；跨任务误用拒绝。turn_id 与终态计费一起登记，不把“已收到 turn_id”当作“已计费”。
- `active_method_inputs` 从活动实例绑定读取确切 ID/版本/摘要；动态 covers 直接使用该入口。同名新写入但未验收的成果不能替换本轮输入；已取消的实例不能验收。固定专家原读法保留。
- 独立 PostgreSQL 全量 `1344 passed / 92 skipped / 2 既有 collection warnings`，599.87 秒（`/tmp/luna-method-planner-full.txt`）。全量启动后增加父计划版本重试守卫及一例测试，五文件最终 `139 passed`（`/tmp/luna-method-planner-final-focused.txt`）；不能把这一例声称包含在前述全量里。最终全仓 ruff 与 app 目录 pyright 0 errors（`/tmp/luna-method-planner-{ruff,pyright}-final.txt`）。早期规划/验收/计算三文件 98 通过不替代最终证据。
- 仍未开放 v3，未改生产、Jenkins、qwen 候选、采单或 Wind（仍 3/5）。后续继续 Advisor/Runner 真正派发与原子收尾、按可信执行上下文进行 MCP 每次调用鉴权、页面/底稿/报告统一视图、完整最终交付。当前用户/沙箱级 MCP 令牌无法单靠 args.task 证明本轮身份；conversation/market/NO_SCOPE 入口也须覆盖。不要靠全局关闭工具误伤其他会话，也不要持 Task 长事务调用模型/远程文件。规划 Attempt 的完成不能让初次方法的范围检查误以为已经执行过方法。接续说明在 docs/method-execution.md。

## 2026-11-02 WP-10 单元 3 第三段：动态执行循环与事务收尾

- 基线 b6784a7493956d026e89377f8f91200f68349263，继续 feat/method-execution-view。用户要求休息期间持续推进可独立开发验证的部分；发布归 qwen，作者内容/真实验收与许可确认留待本人。本轮无部署、线上修改、Wind 调用，3/5 不变。
- Advisor 接 MethodPlanner 与任务视图，开步绑定输入；首个方法范围检查不被规划/定数据完成跳过。远程验收与文件指纹在锁外，结果/费用/事件/游标/返工/交人与最终 handback/Outbox/成功均原子提交。最终核全 final_outputs；重启末尾不重发模型。
- 显式输入 pin 返工失效后，派发先拒绝，再有界重规划重绑同一上游已验收新版；方法/顺序/依赖/工具及旧收据不改，已完成前缀不可改。额度用尽明确失败。取消后的费用只补一次；旧回执不得撤销新问题或释放新项目占用。验收异常/绑定不符不收成果但结算已发生费用。
- 专项 97 passed（/tmp/luna-method-advisor-rebind.txt）；追加迟到提问保护后 Advisor/Runner 50 passed（/tmp/luna-method-advisor-late.txt）。全量原始结果 1365 passed / 1 failed / 92 skipped / 2 既有 collection warnings，480.01 秒（/tmp/luna-method-advisor-full.txt）：usage 取消用例在立即取消终态后抢先读取费用，单测复现同一时序失败（/tmp/luna-method-advisor-stop-repro.txt）。按既有 0034 的先取消、迟到费用另结算语义，测试改为等待对应 turn 的 late_cost 回执，保留所有金额断言；使用量与动态 Advisor 两文件 23 passed（/tmp/luna-method-advisor-stop-final.txt）。最终全量留待下一段合并后重跑，不把失败日志冒充通过。ruff 与 app cwd pyright 通过。
- 既有 ScriptedAppServer + 真实 Runner 验证 RECEIVED→规划→方法→交付；是预录工程证据，不能替代真实模型/数据验收。v3 准入仍关闭；下一段接 Runner 审批、ExpertDesk/文件/报告/UI 视图及工具入口每轮身份。
- 权限准备：0.155.1 MCP 自动传 _meta.threadId、callId 与 x-codex-turn-metadata.turn_id，但不能只相信客户端声明；应核已认证 app-server 上报的调用事件、活动 Attempt 和工具子集。隔离容器 --network none、假 Responses/MCP 的实测 /tmp/luna-method-mcp-scope-probe-events.txt，无真实模型/数据调用。另证每线程 http_headers 独立，已加载线程直接 resume 忽略新头，unsubscribe 后 resume 才更新（/tmp/luna-method-mcp-scope-probe-resume.txt）。不能假定热更新生效或全局禁工具。初稿无首个 turn 导致无 rollout、未标只读导致等待审批超时的日志保留，成功探针才是依据。


## 2026-11-02 WP-10 单元 3 第四段：计划与当前成果的读取面（未发布）

ExpertDesk、底稿、文件定位、Runner 审批定位与孵化测试成果读取共用任务执行视图。未接受计划时只展示作者身份，不虚构步骤；接受后展示选择理由、计划版本/调整历史、当前有效成果版本。返工失效的历史仍可查，但不再进入当前底稿；动态底稿包含全部 final_outputs。规划/定数据的审批不冒充下一方法。固定专家保留原视图。真实 v3 准入仍关闭，MCP 工具权限正在接续。

数据库相关回归 52 passed（/tmp/luna-method-views-focused-final.txt），类型检查 0 errors、ruff 通过。前端从隔离数据库导出的实际动态响应验证 Zod 契约，完整 377 passed / 2 skipped，typecheck/lint/i18n/生产构建通过（/tmp/luna-method-views-web-*）。最初三条新增组件测试使用了不匹配的会话标题，修正 fixture 引用后通过；没有放宽业务断言。后续全量后端验证统一记录在下一段，不把此前全量结果当作本段证据。


## 2026-11-02 WP-10 单元 3 第五段：实际轮次工具鉴权与跨层验证（未发布）

- MethodToolGuard 已接入真实 MCP HTTP 路由，校验沙箱/线程/轮次/callId、Runner 观察的工具事件与完整参数，再校验活动 Attempt 的已接受计划、输入和工具子集。覆盖 conversation/market/no-scope、取消后省略 task、旧轮次、跨沙箱、元信息伪造及底层 run/session 注入。账户存在动态历史后不回退无来源校验；普通聊天使用既有 turn/accepted(by=user)。数据读取继续走既有任务 grant，不代用户确认。
- Runner 在发送前捕获活动 Attempt，以 turn/start 响应写 refs.runtime_turn_id；费用去重的 turn_ids 仍与结算一起写。绑定失败中断并保留用量。规划只读且不挂用户执行环境；原生审批核实际轮次/活动方法，重复请求和已有 grant 不覆盖作者 deny。动态定数据无 MCP 时失败，不假装数据齐全。规划只接收有界的已钉资料元信息及请求状态，不注入未读内容或数据授权凭据。
- 固定专家继续返回原 HTTP 形状，修复第四段额外 plan:null 导致契约 fixture 不一致的问题。没有放宽固定契约或更新基线 fixture 来掩盖差异。
- 完整后端回归 1396 passed / 92 skipped / 2 既有 collection warnings（508.68 秒，/tmp/luna-method-runtime-full-final.txt）。全量启动后补了规划数据元信息及跨层测试，相关四文件最后 44 passed（/tmp/luna-method-runtime-contract-final.txt），含真实 Runner websocket → MCP HTTP → 隔离 DB → 合成 knowbase → 最终成果的验证。最终 ruff/pyright 通过（/tmp/luna-method-runtime-{ruff,pyright}-final.txt）。
- 首次全量 1387 passed / 7 failed / 92 skipped：6 个是新增测试夹具的 publisher 参数和 recorder 接线缺失，1 个是上述固定接口 plan:null 差异；已逐项修复，原日志 /tmp/luna-method-runtime-full.txt 保留。动态定数据新增测试最初缺 grant_ref fixture，补齐后 2 passed；未放宽断言。
- 固定 Codex 0.155.1 在网络隔离容器内跑合成 Responses/MCP：模型调用元信息与 app-server 事件一致，direct call 没有轮次身份，线程 header 隔离、live resume 不刷新而卸载后 resume 刷新均通过。复跑脚本 scripts/probes/codex_mcp_provenance.py，/tmp/luna-method-provenance-probe-final.txt。不读用户配置/Key、不调用真实模型或 Wind，未改全局沙箱配置。
- 当前 v3 外部准入仍关闭；作者方法库 UI/内容由 agy 负责，后续继续对齐孵化测试的稳定期望与多成果回执，再做真实沙箱/作者流程验收。本轮不发版、不动 qwen 候选、Jenkins、sources、线上任务或采单。Wind 仍 3/5、W0 未决；保底仍巨潮。

## 2026-11-02 WP-10 单元 3 第六段：稳定测试期望与多成果产出（未发布）

- 基线 ee3c40c09d2cffa6ce37773411c5f610fe2a2138，接续 feat/method-execution-view。
  final_results 将作者声明的最终方法别名经接受计划映射到确切已验成果，测试断言仍用
  原 equals/approx/contains。动态测试请求拒绝非最终方法名/实例名，不复制评价引擎。
- 测试产出携 plan_version 和全部 final_outputs（方法/实例/成果 ID、版、摘要/JSON/
  可选文件）；重做失效成果只留历史，步骤 current=false。复用原作者工作区文件读取，
  未读到、过大、过旧和超时有说明。总量限制省内容、不丢引用，旧固定响应字段不变。
  provider 为 knowlab-backend ad076ff2591b296d1a22a5780aa15fafc7915013，父仓
  090c985c64cb78a87cf488a59159f3e8390581b6；本父仓只更新 case-outputs schema 消费摘要。
- 全量隔离 PostgreSQL 1405 passed / 92 skipped / 2 既有 collection warnings，494.43 秒
  （/tmp/luna-method-exports-bench-full.txt）。全量开始后只补真实 conductor 结果落库断言
  及实际编译引用清单的两任务运行测试，四文件最终 11 passed，
  /tmp/luna-method-exports-reference-final.txt；没有把追加测试冒充已纳入该次全量。
  最终全仓 ruff/pyright 0 errors，/tmp/luna-method-exports-bench-{ruff,pyright}-final.txt。
- 新数据库测试发现 UUID 未转字符串导致整份产出序列化失败，已修。初版测试还误设
  非最后实例为最终成果、covers 输入不匹配、假定 JSONB 字段顺序及忽略底稿目录；按
  既有真实契约纠正夹具/断言后通过，未放宽校验器。conductor 持久化验证改用既有独立
  get_test_case_outputs，列表接口本来就不带 2 MB 产出。初始日志保留于
  /tmp/luna-method-test-exports-db*.txt、/tmp/luna-method-test-exports-final*.txt 和
  /tmp/luna-method-exports-final-focused*.txt。
- 实际编译的动态方法引用 v2 清单在同一执行器分别跑两步/三步计划，保持 report 方法
  版本 2、冻结作者快照与任务成果隔离。Driver 仍为预录，不证明真实模型选择质量。
  无迁移、Wind 调用或生产发布；v3 准入保持关闭，待 agy 作者页与真实沙箱完整联调。
## 2026-11-02 补采并发与迟到回包保护（未发布）

基线 8d1d8926b131ec00acf7b92f3d994e52e6914cc7，继续 feat/method-execution-view。
正常远程提采完成后，按 Task → resolution 加短事务锁并重读当前 detail 再追加；
保留同期预切合等页面信息，拒重复请求编号、清单身份漂移及终态任务迟到回包。
本地作者测试不走 knowbase，编号/需求生成/追加一起持锁，两个并发调用不再用同一
编号覆盖。会调用 resume_data 的数据收尾同步使用 Task 在前的锁序，避免反向互等。

knowbase 配套 971747286c2b2329c8738f5735e8d76321651cc9 在原修订锁内核基版，
冲突回 409 resolution_changed。工作台明确转为重读/重试错误，不报 listed。
没有把网络调用放进这些行锁事务，没有新增清单状态、HTTP 字段、迁移或自动采集。

验证：数据/孵化两文件 66 passed（/tmp/luna-proposal-race-focused.txt），随后补充
旧提供方并发双成功守卫 1 passed（/tmp/luna-proposal-parallel.txt）。初次定向 2 个失败
是新测试误用了不存在的 Ledger.cancel 和多传 KnowbaseRefused 参数，已改用真实
request_cancel 和实际构造器；原日志 /tmp/luna-proposal-race-first.txt 保留。
最终全仓 ruff/pyright 通过（/tmp/luna-proposal-race-{ruff,pyright}-final.txt）。
最终全量 **1411 passed / 92 skipped / 2 既有 collection warnings**，591.87 秒；
日志 /tmp/luna-proposal-race-full.txt，包含本次全部新增案例。

边界：提供方的旧基版保护必须一起验证，工作台不能修复旧提供方已写坏的远程清单。
远程成功但回包丢失/本地落库前退出仍可能使重试收到 need_key_taken；没有实现操作
收据与自动对账，不声称跨服务 exactly-once。用户确认/修改与新提采同时发生的完整
合并仍是下一恢复单元。没有修改线上采单、保底专家、qwen 发布候选或 Wind 预算。


## 2026-11-02 WP-10：受控动态孵化受理与完整执行链

KnowbotTestService 增加默认关闭的构造期联调能力 dynamic_methods，生产 bootstrap
不传此选项，普通 API 请求不能开门，商城 pack_from_manifest 仍拒绝 v3。受控受理
先校验目录/引用/策略，再写既有测试表；Conductor 从已接受请求重建只读测试目录，
Ledger 钉为 knowlab-test，复用实际 Runner/规划/Attempt/验收和多成果回收。
没有新的执行器、HTTP 字段、配置开关或数据库迁移。关闭新受理不影响已有请求幂等
读取与恢复；取消用原取消链，开跑前取消后不建任务。

新增 7 项隔离数据库测试，不使用 SQL 改任务快照；实际编译的非财务引用目录在同一
正式流程选择两步/三步计划，核最终别名、方法版本、任务摘要与已验成果。覆盖默认
关闭、幂等、取消，以及摘要/定义/编译器篡改入库前拒绝。回复来自脚本 AppServer，
并非真实付费模型或金融内容验证。固定测试、Ledger 和动态多成果回归合计 52 passed，
72.89 秒，/tmp/luna-incubation-regression.log。ruff/pyright 通过，日志
/tmp/luna-incubation-{ruff,pyright}.log。编译覆盖修正带来的两份跨端合成快照已更新，
此前运行侧相关八文件 111 passed（/tmp/luna-coverage-runtime-regression.log）。

本轮没有重复跑后端全量；上述是针对实际改动的回归。没有发布、开启线上 v3、修改
本人暂停采单或新增 Wind 调用。下一步仍需真实作者/沙箱联合验收才决定生产准入。
回退应用无需回退 schema，保留测试及任务记录；默认关闭只控制新受理，不能删除
已受理任务的快照来回退。agy 财务内容继续待下一轮交回，不将测试夹具正式入库。

## 2026-11-02 #52 数据读工具发现故障修复（合入原整合候选）

基线 cb597459ef3ce0a9507657e2cf55609c0f924f8e。用户要求修复后并入现有发版；
qwen 负责 Jenkins/集群，本轮只读诊断线上，不操作委托/确认单/等待提问。
真实工作台 Pod 对配置的 knowbase HTTPS /api/mcp/data 返回 404 text/plain；
knowbase 本地同路径无凭据返回 401 JSON，双方签名公钥指纹与 issuer 一致。
根因是 service-access 模板漏路由，DataRelay 又吞异常返回成功的残缺目录。
官方 app-server thread/read 显示另外两次拒绝分别是读取当前会话、compute 空 inputs；
不能由这些正常拒绝推断任务绑定坏了。mcpServerStatus/list 实测当前线程仅 6 个
项目/提采工具，没有 9 个数据工具。未读内部 SQLite，未输出凭据。

补 knowbase 内部 HTTPS Exact 路由（sunmoonun 同批）；客户端校验 HTTP/JSON-RPC，
只对 notification 接受 202；中继失败返回 -32003，只有完整目录可进入有界缓存，
按提供方/身份/沙箱/签名上下文隔离并返回副本。未配置数据服务的安装仍支持项目工具。
日志只有故障类别/HTTP 状态，不写令牌/上游正文。不改数据授权、计算校验或任务状态。

验证：35 项发现/客户端测试通过；包含它们的工作台定向集 124 passed in 10.71s，
日志 /tmp/luna-data-mcp-regression.log；提供方真实隔离 DB MCP 8 passed in 7.38s，
/tmp/luna-data-mcp-provider.log。真实 knowbase ASGI/JWT→knowbench HTTP 客户端→中继
发现 9 个工具，/tmp/luna-real-mcp-catalog.log（脚本 /tmp/luna_check_real_mcp_catalog.py）。
ruff / pyright 通过；部署模板用真实 config/Jinja 渲染 dev/test，1 个测试通过。
首次定向命令使用了不存在的 test_workbench_run_reader.py，收集前退出，已改用真实
存在的 MCP/compute/method_runner 测试集重跑；不将那次空跑算通过。未跑后端全量。

剩余：qwen 按同一交接更新入口声明和镜像；真实 tools/list 及已有线程目录刷新后
验收，旧任务的用户提问继续由本人决定。无迁移、Wind 真实调用为 0；当前不是生产
已修复的结论，不能用本地通过代替发布后检查。回退保留用户材料与记录。


## 2026-11-02：kimi 方法库复核后的通用验收修复

基线 15643e2（含 Luna #52 数据 MCP 路由/发现修复），分支 fix/kimi-method-validation。
AcceptanceRule 可选扩展：computed.expected_value 校验记录值和交回值均在目标容差内；
computed.input_keys 将同名数值字段绑定到本任务计算记录中的已解析操作数；
each_has.non_empty_keys/unique_by 检查非空字段和联合唯一性。没有财务专用执行器。
未使用的新字段不进入序列化，旧快照不增加默认键。旧运行器 extra=forbid 会拒绝新规则，
不能在未升级的运行器上发布依赖这些检查的方法。

验收：运行侧验收/计算/目录/计划 231 passed、1 skipped；最后追加两条容差及数值唯一性
反例后，验收/计算子集 150 passed（含新增两项）。隔离数据库 method_acceptance 2 passed。
ruff、pyright 通过。运行证据 /tmp/kimi-method-runtime-tests.txt；未用生产数据或 Wind。
父仓需同步 dynamic-methods v1/v2 schema，knowlab 同步 consumer lock。
回退应同时回退依赖扩展的候选方法版本和编译/UI，不删历史任务或覆盖历史快照。


## 2026-11-03 旧 waiting 不能默认缺数续跑

旧解析 waiting 没有可证的当前采集执行状态时，确认路径拒绝推进并要求重新核对。
用户显式去掉条目仍尊重其决定；实际补采结束 after_supply 的缺项仍走原交人路径。
定数据提示区分 original/pages/text/table：文字不等于事实编号；compute 的上游需
相应事实读法，按作者条件和本题范围提数据，不机械加预测数据。
52 项规划/实际 HTTP/隔离 DB 回归通过；另新增 HTTP 负例 1 passed：旧 waiting
确认返回422、无签新授权/无 demand、任务仍 WAITING/DATA；解析订正后确认正常派单。
ruff/pyright 通过。没有改旧任务成功状态或冻结验收；旧专家缺数值验收另需作者新版。

独立热修复以 #58 为基线，仅移植上述修复；原开发提交 6ef1211。
