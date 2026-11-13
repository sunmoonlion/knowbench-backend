# 2026-11-13 技能包与自动发现扩展（待本地发布）

后端 ZIP 校验/不可变资源、自动目录与按需读取、逐次撤权核验/读回执、专家隔离、契约更新。没有新增 DDL，迁移头 0040。
用户明确用途为 KnowBench 网页聊天／工作；自动可用只提供名称用途，正文和附件按需读取。
普通聊天不用原生 Skill 目录，不开放命令执行。按轮冻结版本与范围，停用不能擦除历史正文。
代码和实际边界见 knowbench-backend/docs/managed-capabilities.md。
验证：相关 PostgreSQL/HTTP 18 passed，单元/内核/分层 43 passed，网页组件/契约9 passed；
原有 runner/路由/账房回归57 passed。Ruff、Pyright、Node24 TypeScript/ESLint通过。
固定Codex0.155.1断网容器验证 lazy reference read，
本地证据 /home/zymun/sunmoon-probe-runs/skill-package-20261113。未发起真实模型付费调用。
Agently完整包已在本地桌面 agently-skills-384a6fb0.zip；尚未导入任何真实平台账号。
发布仍沿用上次隔离分支 capabilities-release-20261113，仅挑选本次改动；不带其他未发布开发。

# 2026-11-13 发布前空配置阻断修复

真实固定Codex补验发现：未预置受管MCP时仅设置enabled=false仍报invalid transport，
Jenkins #91已在部署前停止，现网未改。禁用配置改为完整无凭据传输定义（127.0.0.1:9、
enabled=false、空工具列表、required=false），固定版不会连接这个禁用地址。
app/scripts/probes/codex_capabilities_probe.py现在从无MCP配置启动，真实验证A/B切换和
再次禁用；三轮均通过，禁用后无受管工具暴露。此修复必须包含在本地重新发布候选。

# 2026-11-13 本地增量发布候选

所有者授权：到本地按原Jenkins流程发布；外部MCP目录先空，仅发布入口和纯指令Skill。
工位 /home/zymun/worktrees/capabilities-release-20261113，分支 capabilities-release-20261113。
现网基线 1d1fdda4e9d1134d6040abd5c26d0f0d64a36b3f，平台基线Jenkins #90回执a81adda3。
仅摘取58ea7ce；两处冲突保留现网Runner构造，仅增加capabilities，不引入fixed_as_plan_owners。后端单元/内核/分层29项及真实隔离DB/迁移/HTTP/原runner回归79项通过，Ruff和Pyright通过。
日志 /home/zymun/sunmoon-probe-runs/capabilities-release-20261113/；迁移0039→0040。
冻结专家新增MCP/Skill绑定仍未实现，不因发布而宣称完整接入方案完成。
以下qwen开发记录保留为来源证据，不能把原基线当作本次发布基线。

# 2026-11-13 统一能力三个入口首期代码（未部署）

分支 qwen；后端基线 3cb341fedf9516ee949a477a555ced22017b448c。
用户授权三个前端部分及所需后端开发，按默认协作规则提交推送；未授权部署或付费验收。
当前交付：连接/纯指令 Skill 资产与加密凭据、会话默认快照和版本选择、限定会话 MCP
网关、精确参数审批与持久回执、runner 空闲边界准备和按轮暴露。提供方响应模型、
导出 Schema/样例与网页消费者锁配套。迁移 20261113_0040 接 20261106_0039。
详见 docs/managed-capabilities.md，包含配置、发布顺序与回退；尚未修改生产配置/数据。

边界：审核目录 HTTPS Streamable HTTP MCP（无认证/Bearer）和纯 Markdown 指令。
专家面板只读投影冻结步骤声明；方法的新连接/Skill 需求字段、绑定及步骤执行未完成，
不得将聊天选择当专家授权。OAuth、原生 SaaS、stdio、技能脚本包/附件/市场不在此交付。
冻结财务专家保持原契约；这是首期代码，不是整个接入方案或线上验收完成。

验证（隔离测试均使用合成服务，无付费调用）：
- 新 DB/实际 HTTP 契约/迁移回归 14 passed：/tmp/capability-db-verified.log。
- MCP 协议与 runner 忙碌/未知线程保护 21 passed：/tmp/capability-final-unit.log。
- 原 runner/路由/记账回归 57 passed：/tmp/capability-existing-final.log。
- 较早的新能力+数据计划+内核/分层组合 46 passed：/tmp/capability-backend-final.log。
- Ruff、Pyright（/tmp/capability-pyright-verified.log）、diff 检查通过；并非全后端套件。
- 固定 Codex 0.155.1 真实二进制，无网络512MiB容器，脚本
  app/scripts/probes/codex_capabilities_probe.py：同线程从明确禁用转为启用；A轮只见A别名、
  B轮只见B别名，认证头同步轮换；显式指令正文到达模型，未开放本地执行，登记工具可调用。
- 独立只读复核的阻断项已修复：默认快照、准备竞态、忙碌时换token、外部Schema引用、
  失联回执、超过100条去重、网关注解、迟到scope/turn通知。复核不替代真实服务业务验收。

发现并适配：新空线程先注入既有模式说明物化历史再重载；metadata idle可用，固定版
includeTurns=true分页路径不支持；environments=[]原生Skill不加载正文，所以纯指令显式输入。
已消费批准先提交dispatching再访问外部；进程断连查账转unknown而不重发。只读也逐次批准。
早期临时PG停止导致旧回归报连接错，重建磁盘数据目录的256MiB隔离库后57项完整重跑通过；
不把早期失败记成全绿。未操作原有服务。后续需发布审核目录/网关配置、迁移和同批API/
runner/web镜像，经Jenkins与活动任务检查后另做真实账号及浏览器验收；不自动代批操作。
没有收到明确 token/费用预算；当前真实供应商调用费用为0。测试临时输出不含真实凭据。

# 2026-11-04 定数据重复调用保护（独立发布候选）

分支 fix/data-planning-progress，基线 25956816edbd639e2e7b226f2776d81d4a753b09（#68）。
首次请专家的 data-plan 与正式作者步骤分开：提示明确空授权清单不是全库无数据；
来源发现为空仍可提公开报告需求；不在提需阶段分析、翻页取财务数字或反复联网查值。
运行器按服务端真实尝试识别阶段，完成通知按 item id 去重；同参数同结果第3次、
第3次内置搜索、第40次工具完成或180秒有效执行时间触发中断。等人时间不计入时限。
正式方法步骤不受这些新增阶段上限约束，未全局关闭搜索，未改专家包/公式/验收/预算。

确认 turn 已结束才进入 INPUT，说明原因并提供显式重试/停止，调度不会自动重试。
停止未获确认走既有平台失败与 turn_open 项目占用对账，保留已知费用并标待核对；
不伪称已停止、不自动开第二轮。数据计划失败不变 ready，不把部分需求当分析完成。
只读投影显示定数据，正式步骤全待开始；无步骤编号的暂停卡仍可见。

验证：L1+L2+L5 82 passed（含真实隔离PostgreSQL、进程内WebSocket、既有数据/步骤/
review/动态投影回归），日志 /tmp/luna-data-planning-regression.log；补充显式重试、
停止测试4 passed，/tmp/luna-data-planning-resume.log。Ruff、Pyright、diff检查通过。
初轮新测试的 fixture 导入被lint移除、确认后的VALIDATING状态断言写错，已纠正；
首次扩大命令误写不存在的测试文件而未运行，以上为修正后实际结果，不算首次全绿。
未进行真实模型/Wind调用或生产写入。无迁移/新配置/契约字段。只在候选工位开发，
开发主工位K29未提交隔离改动保持原样。后续同批部署knowbench API/runner/web，Qwen
走Jenkins并核活动turn，不能中断正在付费的任务。线上模型能否一次正确提单待发布后验证，
不把有界暂停说成已保证提单成功，不自动启动付费验收；新规则在新data-plan turn生效。

# 2026-11-03 第二轨准备：通用批量计算（未发布）

所有者将本轮发布交给Qwen，Luna继续第二轨开发，待发布后验证并冻结固定专家备用。
同一compute增加batch请求：1–50项、整批最多500个输入引用；一次去重授权读事实，
允许batch:key引用前项，逐项保留普通c编号和计算事件。单项形状与验收凭据不变。
先求值再在同一事务记账；任一表达式错误、写入异常或MCP结果超限则整批拒绝/回滚；
供数返回后再次核任务可执行状态。不同任务计算编号和其他用户任务均拒绝。

验证：定向单元/MCP/真实PostgreSQL/动态证据到成稿共160 passed、无跳过；全仓Ruff、
Pyright通过。测试模型与数据供应端为合成替身，不是Kimi/Wind真实业务验收。
开发基线（本次batch修改前）全量1691 passed/92 skipped/2 collection warnings；
不能将其记成本次修改后全量。日志 /tmp/luna-batch-final.log、
/tmp/luna-second-track-full-bench-20261103.log。

无迁移、新配置或供应商调用。泛用算术仍由作者指定，不内置财务方法、不放宽原判据。
这是长期共用能力，未加固定执行兼容分支，不属于K31演示后删除层。新能力需要模型
实际选择batch才减少往返；未承诺真实勾稽耗时。第二轨真实作者装配与业务联验待做。
本轮不改Qwen当前sources、不部署、不启动新的付费测试。后续发布需固定专家旧调用
回归和真实tools/list确认；工具定义缓存需按既有新线程/刷新流程更新。


### 合入已发布 #66 的历史记录

# 2026-11-03 测试10收尾修复（本地回归，待Qwen发布）

读取状态时依据尚保留的任务账只读重建已收尾用例花费，修正旧超时轮漏计显示；到期清理前存妥汇总。新增回归模拟旧run.spent漏计，GET不改历史行、清理后金额仍保留。最终DB34项及Pyright/Ruff再次通过。

真实test10整轮3600秒超时，c1/c2完成，c3已消费12.011364元却未计入run.spent。修复完成、超时、取消的汇总：按已收尾用例的task.budget.used对账，行锁串行化、幂等、不叠加旧快照；record_late_cost仍按turn去重，并同步已归档用例费用。保留结果及清理后的累计金额。全部用例已记录时优先结论，不能因下一次调度晚到而报超时。时限仍为整轮，不改v1契约。

test10 c2“本文不含评级、目标价或买卖建议，如何判断留给您。”误拒：扩大完整声明的明确否定词与限定尾句，不豁免数字目标价、含糊续句或其他字段的建议。36个声明正反例通过；真实隔离DB测试34项通过（包含3项新增：跨用例费用、超时/取消、迟到费用重复回报、清理、晚到调度）。Ruff/Pyright通过。未修改线上测试、费用、专家版本；未启动收费重测。

规则：C-D1费用主档仍task预算，run只是汇总；C-C10不改时限语义；C-L作者四条恒等式及1元阈值不变。第一题三期A-TLE只是辅助检查，不能作为原式A-(L+E)验收，需独立专家修订；本修复不宣称解决金融方法语义。

# 2026-11-03：否定声明误判修复（同批候选）

test9 c2 `b0c0a388-8740-4f25-a1f5-80f17e3e10d4` 成稿第二次因“不给买卖建议、评级、目标价，结论留给用户。”中的“目标价”误拒。按所有者“修复啊”，只豁免完整、明确的非建议声明；声明中带数值、冒号定价、双重否定、模糊续句仍按原词表拒绝，其他句/其他字段的建议照查。逐个文本叶子判断，不跨字段拼出否定声明；保留既有path、结构规则和原交回物，不改历史结论。

新增29项正反例；含前一补丁合计131项单元回归通过、Ruff/Pyright通过。真实c2第二次交回未改文字，在本地通过原JSON/必填/非建议三项检查；不是重跑文件交付或整轮验收。原文仅本机/tmp/luna_test9_c2_disclaimer_evidence.json。没有新增模型调用/测试费用、没有部署。待Qwen按交接13.6新候选发布；旧53da215候选不含此修复，需替换。此确定性豁免不是通用语义判断，未涵盖的歧义表述仍保守拦截。

# 2026-11-03：步骤验收反馈（本地修复，待 Qwen 发布）

真实 test9 c1 `9364e47e-3fba-4382-8f1a-4ab0c676bdda`：第5步第二次已通过；第6步两次均少最外层 `}`，04:06:43 UTC 后等待用户，随后由用户取消。原始失败留在事件中，不改写、不补成成功。只补括号的离线诊断还发现上游8条被改写为10条，covers有18处缺失/额外项；quoted只验证引用存在，不能证明语义支持。

平台修复：JSON 解码改用标准 raw_decode 处理带文字的对象与字符串内括号；失败反馈包含位置及未闭合括号，不自动补全、不输出原文。Advisor.compose 从冻结 StepContract.acceptance 传递完整既有规则，首次即知道枚举、容差与 covers 原文身份对应；不扩大工具/数据/预算，不改历史专家包。C-A1/C-A9：共用执行器、确定性检查；C-D14：旧证据不改；C-R10：离线通过不等于专家业务验收。

本地单元回归102通过，Ruff通过，Pyright 0 errors。实际两份失败原文离线重放均明确报告缺 `}`，仍拒绝；补括号只作诊断，未写回平台。原文在本机 /tmp/luna_test9_official_evidence.json；不作为生产数据夹具入仓。新付费测试未启动，2.6/公开第1版未改变。剩余：发布候选集成回归、Qwen发布；第6步不同口径/仅部分引文支持的问题需专家内容复核，不声称本补丁能自动判语义。

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

## 2026-11-02 第二轨：作者测试准入与真实模型隔离执行

独立分支 feat/dynamic-author-intake，不改变 qwen 正在发布的 31e3701。将仅能测试
构造的布尔开关替换成部署侧精确作者 subject 列表 KNOWBOT_DYNAMIC_TEST_OWNERS，
bootstrap 接入。默认空，HTTP 不能指定；仅该作者的新动态孵化测试可以受理，
目录/方法摘要、电脑归属、预算与数据授权继续原路径。关闭准入不影响已有 run
的幂等恢复。商城 v3 仍关闭；无新表、迁移或请求协议字段。

HTTP/动态生命周期 17 passed，固定测试/规划/MCP/多成果回归 52 passed，ruff 与
pyright 通过。日志 /tmp/luna-dynamic-intake-{tests,regression,types}.txt。

另外用实际用户沙箱 Codex 0.155.1 / kimi-k3 做了两层验证：两种题目生成不同计划，
均通过实际校验器；隔离 DB 的正式受理→Conductor→Runner→Advisor→验收→结果
回收，两条合成文档任务 2 passed（166.12 秒），选择 2/3 方法。8 个实际模型 turn，
包括 1 个非法 JSON 规划被拒并在次数上限内重试。没有替换助手输出或修补 JSON。
模型用量采用实报值，金额只是既有配置价目表的估算；不宣称厂商实际账单已核对。

验证边界：本地连接登记为合成测试账号，探针转接各 turn 至独立真实沙箱线程，
合成文档全部在题面，无数据 MCP/relay/Wind 或 PDF 检验；不是线上任务端到端验收。
原件 /tmp/luna-dynamic-live-{results,engine}.jsonl 与 engine-tests.txt，持久证据
归档于 sunmoonun 总纲/数据拓扑重构/第二轨联调/2026-11-02-创意入口与动态测试。
没有更新沙箱、续跑旧任务或代点采单；生产仍需 qwen 发布本增量后本人正式测试。


## 2026-11-02 第二轨：商城动态专家准入与原入口衔接

合入 #57 上的读工具预检修复 f15e492，保留动态作者测试增量。新增独立
KNOWBOT_DYNAMIC_MARKET_OWNERS 精确 subject 配置，默认空；新 pin/委托均核，
任务冻结完整方法目录（展示头不再代替目录），已有任务幂等恢复不受关闭准入影响。
v3 依次走任务规划与定数据；缺数明确可走供应商查询（包括 Wind），不机械要求新采文档；
方法要求 PDF 时仍不可用摘要替代。孵化格式消费锁同步，真实表轴可达模型 material。

合并后读工具/准入定向 25 passed；商城 HTTP、旧专家委托/数据、引用方法运行、
动态推进与孵化锁回归 83 passed（真实隔离 DB，无跳过），ruff、pyright 通过。
日志 /tmp/luna-market-{merged,regression,pyright}.log。非生产准入已开启的结论；
不把作者候选方法标为接受，不改 qwen 当前固定候选、不代点业务确认。

## 2026-11-03 WP-1：精确小数、公式比对与披露参考回归

修复 compute 的小数字面量先经 AST float 舍入的问题：
9007199254740993.01 - 9007199254740993 旧值 1.0，现值 0.01。
按原始字面量读 Decimal；公式 AST 比对使用精确规范值，保留 1.00/1e0 等价，
不同末位不再混同；指数限制在 Decimal 默认数量级范围，避免无界字符串展开。
text_of 不再 normalize 导致输出受外部 Decimal context 精度截断。

新增 financial-reference-cases.json：茅台年报、宁德中报 14 项原值与 13 道公式参考，
记录公开 PDF 链接、SHA-256、页码、原单位和口径。仅工程夹具、待第二人复核和作者
接受；fact ID 为测试替身。实际 KnowbotCompute→计算事件→验收验证正常结果、错数、
空结果与缺失事实；不将财务公式硬编码到通用生产计算器，不改变旧任务冻结规则。

计算/验收/引用目录及隔离 DB 方法运行合并回归 189 passed，无跳过，3.87s；ruff
通过，pyright 0 errors。日志 /tmp/luna-wp1-all.log、/tmp/luna-wp1-pyright.log。
没有生产 DB、采单、任务或方法版本操作。qwen 当前发布候选不变；本提交待后续整合。

### 合入已发布 #66 的历史记录


## 2026-11-03 旧 waiting 不能默认缺数续跑

旧解析 waiting 没有可证的当前采集执行状态时，确认路径拒绝推进并要求重新核对。
用户显式去掉条目仍尊重其决定；实际补采结束 after_supply 的缺项仍走原交人路径。
定数据提示区分 original/pages/text/table：文字不等于事实编号；compute 的上游需
相应事实读法，按作者条件和本题范围提数据，不机械加预测数据。
52 项规划/实际 HTTP/隔离 DB 回归通过；另新增 HTTP 负例 1 passed：旧 waiting
确认返回422、无签新授权/无 demand、任务仍 WAITING/DATA；解析订正后确认正常派单。
ruff/pyright 通过。没有改旧任务成功状态或冻结验收；旧专家缺数值验收另需作者新版。


## 2026-11-03 第二轨证据与文件交付联调回归

新增 test_method_evidence_delivery_db.py 与 test_source_query_delivery_db.py。
动态计划选择既定方法→真实RunReader签名/任务grant校验→真实compute事件→
sourced/computed/quoted/covers验收→实际临时Markdown文件→底稿投影→handback。
合成模型/供应商端口，真实隔离数据库与临时文件；不是生产Wind/财务专家业务验收。
正常案例通过；计算值/原值篡改、假引文、空引文、明确缺文件均按作者一次返工后停下，
不生成最终成果与handback。已有软文件检查的“无法核实”情况未改，不声称所有通道离线都硬失败。
供应商source_query从request_data到HTTP确认/需求下发/交付重解析贯通；模型伪造request_id
不被接受；确认前无提交；成功钉text读法后续跑，失败显示missing，不偷偷再次查询。
最终新增8项通过（13.44s），关联MCP/方法/供应商回归50项通过（68.30s，含新增中6项）；
ruff和pyright通过。原始输出 /tmp/luna-second-track-evidence-final.log 与
/tmp/luna-second-track-evidence-chain.log。初期测试夹具必需字段/状态断言纠正不计产品修复。
只加测试，生产代码/契约不变；qwen本次发布候选不动。专家v2仍待编译修复发布后测试交付。

### 合入已发布 #66 的历史记录

独立热修复以 #58 为基线，仅移植上述修复；原开发提交 6ef1211。

## 2026-11-03 成稿读取完整性修复

基线 c0486f9；read_bytes 原先不核返回字节与 size/eof，截断响应可能被测试报告标为
完整，或在缺少验收摘要时作为产出写入 knowbase；老 link 的 digest 回退也会对半份文件算哈希。
先加12个故障响应回归全部失败，再补严格 Base64、非负整型 size、全量字节数及可选 eof 校验。
错误沿用 AgentServiceFailed：产出写入留 pending 重试，测试成稿读取留错误，旧 link 摘要回退返回未知。
完整空文件与未带 eof 的旧响应仍接受；已有文件存在性“问不到→软检查”规则不变。
真实隔离数据库证明有/无验收摘要两种情况下都不归档半份文件，恢复后写入原样完整内容。
文件/归档/动态测试导出/动态证据成稿 66 passed（48.89s），ruff/pyright通过。
日志 /tmp/luna-deliverable-integrity-before.log、/tmp/luna-deliverable-integrity-after.log；根仓保留副本。
无迁移、契约或配置变更；只影响读取成稿的错误处理。未移动Qwen当前候选，后续集成发布。
不校验Markdown与JSON语义一致，不宣称财务数字验收完成。专家v2仍等线上编译修复。


## 2026-11-03 所有者确认新版财务测试

测试运行端支持可选row选择器，缺行/重复行/非法形状失败；值不按字符串强制转换，True不等于1。134项规则/契约/计算回归通过，真实DB与runner两例覆盖持久化和重复行拒绝；供给/授权/续跑等扩展DB78项通过，接口样例最初因两个source_query:null失败，核对后与web夹具同步并单独复测1项通过。与knowlab provider同批发布，旧摘要不变。

## 2026-11-03 动态方法成稿实际路径冲突

基线41263f1，所有者要求继续第二轨。动态计划原先只比较声明路径，实际placed()把
所有文件放本次目录并只取basename；analysis/report.md与forecast/report.md会覆盖，
作者成稿也可能覆盖平台交回.md。5个故障先复现失败，后补实际落盘目标（含大小写）
和保留交回文件检查；复用placed()与HANDBACK_FILE，不新增第二套文件命名算法。
原声明路径防冲突保留；仅检查本次选中的方法，不因未选可选方法同名拒绝整个目录。
规划说明同步实际文件限制，不能让模型擅自改作者文件名。

101项规则/投影/目录/成稿回归通过，43项真实隔离DB联调通过（55.19s），
定向ruff/pyright通过。新增DB反例证明不派发方法、不写成果、不交付；规划实际费用
照记、预算预留归零、规划重试有上限。日志/tmp/luna-method-file-{before,unit,db,types}.log。
无新迁移/配置/契约；固定专家路径不变。读旧动态计划同样复核，不自动改冻结路径；
历史冲突计划会被阻止继续执行，需作者修改方法文件名并另出版本。正常已完成历史不改。
本轮未部署/调用模型或Wind；Qwen的fix/test-row-selector候选不变，后续第二轨集成。


### 合入已发布 #66 的历史记录


## 2026-11-03 真实测试勾稽 compute 变量拒绝修复

准专家2.5测试4第一题：20次compute均因A/L/E/TLE等大写变量被拒，工具声明未告知小写限制。
勾稽约15分钟后预算结算5.176852，第一题累计6.847252超过均分6.66，BUDGET_EXCEEDED。
支持1–32位大小写敏感ASCII变量（不改写表达式/引用），MCP声明与校验共用正则，保留AST白名单。
1项失败复现；修复后124项计算/数值验收/契约通过，6项真实隔离DB HTTP通过，Pyright通过。
线上仍#60，未重跑或改预算；第二题等待作者数据确认。此修复不代表权益抽取缺项/响应耗时已解决。


### 合入已发布 #66 的历史记录

独立候选基于已发布#60，仅移植计算符号修复；候选验证另记回执。


## 2026-11-03 测试6指标精度验收修复

测试6 a5221685… / c1 c13673eb… 的 indicators 两轮均失败。第一轮引文不连续且22项数值截短；第二轮引文已通过，模型原始JSON数字与22条compute记录完全相等，但json.loads转float后丢失精度，零容差比较误拒绝。
只对computed/sourced规则检查的value及input_keys，保留解析中发生精度损失的十进制原值为字符串（计算工具和既有验收已支持）；其余JSON类型保留。验收后持久化也保留精度，不从计算记录填值、不放宽容差、不修改引文规则或专家版本。
验证：真实两轮原文+记录只读回放，第一轮22/22拒绝、第二轮22/22通过数值验收；定向211通过，ruff/format/pyright通过。新增19项精度/持久化/错值/编号与表达式/输入/形状回归。
仅本地修复，发布交Qwen；线上历史不改判，不强制复活c1，不自动追加预算或重测。第二题仍在当前已批准测试中。预测/回测/报告未验收，不能发布专家第2版。
约束：C-D14保留历史证据，C-A/C-L1保留作者方法与验收阈值，C-R1/C-R2仅固定独立补丁候选；无迁移、跨App契约或配置变化。


## 2026-11-03 第二轨：找专家受理幂等与动态执行接线

基线 c804c76。所有者外出，明确继续其他第二轨开发；不改测试10/生产数据/发布钉版。
FindExpertService.start 原先先提交新会话，再交账房；同请求重发因 session_id 摘要变化
返回409且留空会话，并发时唯一约束错误，离线/派发失败也留下半次受理。
现以本人项目行锁串行核同请求，同事务创建会话、任务、事件及驾驶命令；重发复用
旧 session/task，变更预算拒绝；已取消或排队任务重发不复活、不提前派发。
原 task_key 算法与 HTTP 形状不变，旧请求可查回；不清理历史空会话。

新增7项HTTP/隔离PostgreSQL故障与权限测试；修复前先复现5项产品失败（测试辅助函数
曾写错 async comprehension 和事件表名，已纠正后重新取失败证据）。再增加2项真实
运行器联调：公开目录查询→模型结果复核→钉确切方法版本→委托→按本题选可选方法→
交回成果；模型/商城为合成端口，无真实金融取数，不替作者接受财务方法。

最终相关32 passed，无跳过；Ruff及生产文件Pyright通过。日志/tmp/luna-find-regression.log，
/tmp/luna-find-intake-before.log、/tmp/luna-find-backend-types.log；根文档保存证据。
无迁移/配置/契约变更；前端需配套沿用找专家轮次委托键。发布交qwen，当前#65和
180元测试10保持原状。本项属WP-10/原AI找专家路径，不销账X1正式作者业务闭环。


## 2026-11-03 动态实施 G1/G3：能力目录与按题可选计划

新增 dynamic-methods/v3、method-plan/v2，保留旧模型与生成 schema 不变。作者能力显式绑定方法和成果；可选输入只能因未选上游省略，选中则强制绑定，不允许藏起失败或不利结论；所有已选成果必须进入最终交付。条件验收由作者冻结，必需 JSON/输出/安全检查不可条件化。投影到原 StepContract/执行器，不增第二运行器。

覆盖校验要求服务端给出权威任务要求与允许的能力，模型不能自行列一小部分要求后自证完整。应用接线尚待匹配收据完成，缺权威要求时拒绝新计划。118 项新旧目录/计划/真实作者编译夹具回归通过；Ruff 和生产文件 Pyright 通过。未启用线上动态入口，未改固定测试11/正式数据/发布钉版。


## 2026-11-03 动态实施 G3/G4：真实匹配收据贯通委托与计划

找专家v2从公开目录快照生成逐要求覆盖证据；结果读取不写任务，目录故障不冒充无匹配，委托重核版本/摘要/费用/归属/原问题。手选动态专家只核指定版本，不替换。权威要求保存到服务端task输入，重规划验收其摘要；客户端不能自行提供需求证明。作者测试未上架的v3准专家，在原规划轮次做范围核对并形成计划，范围不覆盖留证拒绝执行；不是新增运行器。

本地隔离DB贯通匹配→委托→计划→原执行器，含有无复核、项目新建/原会话、重放与未覆盖测试。领域62 passed；DB与最终构建结果见同批根记录。v2独立方法源经knowlab正式编译器生成合成夹具，消费者核提供方schema/hash与可选输入。未发布、未改测试11、未启动新付费测试；真实作者资产/真实模型语义验收仍待完成。

## 2026-11-03 G7测试过程投影和v2方法断言

基线dbb3887。内部testrun execution接口从原ExpertDesk投影步骤/工具计数/费用/交回物，核owner、run、case、test profile绑定；优先读测试归档文件，不重读共用路径。knowlab provider schema与consumer lock同批。
v2测试请求保留原c1指标/预测断言：仅从当前计划及成功Attempt/输入血缘解析单实例中间方法；未选/重复/失效失败，显式final_outputs仍管最终成果。v1保持只核最终方法。隔离DB13项通过（含原动态intake、重做失效、伪新artifact不能冒充、缺方法断言失败、跨作者拒绝）；单元4项通过；Ruff/Pyright通过。未调用付费模型。
G7仍未隐藏knowbench测试列表，停止/归档等入口未全部迁完，不伪称页面隔离已完成。新准专家待knowlab部署后正式生成；不自动开启市场准入、测试或上架。

### 合入已发布 #66 的历史记录


## 2026-11-03 本人专家列表移除（隔离发布补丁）

基于已发布#67的da89249。新增0036迁移workbench_hidden_experts保存owner/profile列表偏好；DELETE /packs/{id}校验本人可读专家，幂等隐藏；GET /packs过滤隐藏项；重新pin同版恢复。不可变pin和任务快照不删除，不停止活动任务，不影响其他用户或商城公开状态。

隔离PostgreSQL回归56 passed，含跨用户拒绝、重复删除、历史任务仍读、重新请回；Ruff/Pyright通过。顺带修正既有data-needs断言遗漏#67已返回scope_check_required，断言其固定专家值为false，未改变生产契约。API与网页同批，先迁移后部署；发布交qwen，不混入撤回候选。

## 2026-11-04 撤回候选的代码纠正：按声明选择、手选与作者测试

本批在 feat/dynamic-author-intake 开发，先合入已独立交 qwen 的入口/列表删除后端补丁，未移动发布候选。
新增 expert-match/v4：依据原题目标/明确约束和真实公开声明给选择理由、已知限制，不要求方法供需全覆盖或联合可行性证明；仍核目录完整性、版本/摘要、引用原文、费用、身份、原题和幂等。v1/v2/v3历史解析不改。
TaskRequirements 明确 selection_basis；v4允许范围不是覆盖证明，旧字段仅为既有执行器所需的目标到作者能力约束。手选与新建作者测试直接钉原题和作者允许范围，没有伪造匹配任务/收据；旧作者测试无请求摘要时继续按原合同解释。测试原题不被匹配器改成另一个方法方案。
无匹配转孵化沿原入口显式提交，仅传用户原题目标，无推导方法/假缺口；工具权限、数据确认、预算和结果验收保持。

验证：领域/旧契约78 passed；真实隔离PostgreSQL定向81 passed，随后新增作者测试边界和最终手选空匹配标识的规划/执行回归20 passed；孵化提供方契约8 passed。各范围有重叠，不合计。Ruff通过、全仓Pyright 0 errors。真实runner测试用合成模型响应，不冒充真实模型或财务语义验收。无生产写入、付费模型/Wind调用或发布。
日志 /tmp/luna-declaration-domain-final.txt、luna-declaration-db-final.txt、luna-author-request-db.txt、luna-author-request-types.txt。下一步仍需真实作者加工/孵化、匹配质量和业务执行验收；不宣称成熟方法准入或第二轨全完。


## 2026-11-04 AI找专家独立调度（隔离发布补丁）

基于083b2c4定数据守卫补丁。项目内执行与FIND_EXPERT目录查找各一项活动任务：按受信profile分道，项目锁与唯一索引共同约束；终态/turn释放只推进同道，对账可恢复旧跨道queued_behind。0038迁移基于0036，仅调整活动任务唯一索引，不改历史或预算。回退需先排空并行活动任务，旧索引禁止带冲突回退。K29未发布的0037测试项目迁移不在本候选。

查找仍有独立会话、原2元默认预算、原每步900秒时限和取消记账；线程和turn不挂本地目录/环境，沿用read-only/never与封闭目录工具策略。找回只选定，显式委托才排执行队列。RECEIVED正式方法投影为pending，无正在执行的步骤号。项目归档仍核全部任务；普通工作只受执行道占用约束。

隔离DB/受控app-server回归104 passed；步骤/审查/定数据追加32 passed（其中7项与前套重叠）。覆盖RUNNING及WAITING DATA不挡查找、两道各自串行、并发提交、旧队列正常恢复、伪client_context不能选道、同一连接两条线程独立完成和记账、排队无虚假步骤。Ruff/Pyright通过。无真实模型调用、生产改写或专家修改；发布后真实模型匹配仍待核验。


## 2026-11-04 查找目录客户端报错与无效重试修复

现场a65a7e2f：01:34:34开始，find_expert九次失败，API明确TypeError：ServiceClient.request不接受params，目录请求未发出；01:46:58因预算转WAITING RESOURCE，估算3.570116/上限2。旧b51562bb已由用户取消，不能恢复。修正为客户端支持的路径查询串，并用真实ServiceClient+MockTransport测directory_version=2、owner编码、授权、非完整目录拒绝，避免只mock领域目录掩盖错误。

只读在线核验修正后的GET，真实knowmart返回complete=true，一位免费财务分析与预测第3版；未调用模型、未写线上任务。新增查找专属120秒有效执行时限、目录工具失败即停、重复三次/额外命令与联网即停、已上报估算费用达到剩余预算即停。供应商延迟报量或在途请求可能造成金额超限，不承诺绝对硬金额上限。失败不自动重试/追加预算/伪装无匹配。中断确认复用已有保护，未确认保留turn_open等待对账，不放下一项查找。普通作者方法不受此新时限影响。

增量客户端/真实WS隔离DB/既有数据准备与匹配回归28 passed，含确认/不确认中断、费用留存、预算与超时、成功查找。Ruff/Pyright通过。错误原因从既有task/state事件读取，未挪用仅供REJECTED的rejection列。前端不再将连接存在解释成正常思考。候选仍需Qwen发布；不要求改专家包。

## 2026-11-04 查找工具只读声明修复（合入待发布候选）

基线 f065961，包含前一轮阶段搜索配置修复。现场任务 9dc12117-8eb9-411c-960f-5d06a78402d9 与 10762dad-0f03-4d7f-b564-f7492ee8c244 均在 find_expert 调用前被 Codex 拒绝：`MCP tool call requires approval, but approval policy is never`，readOnlyHint=null、durationMs=0、result=null。不是市场返回无匹配，也不是目录请求耗尽2元预算。

find_expert 明确 readOnlyHint=true：只读市场声明，保存不可变目录审计快照；不创建委托、不购买、不变更授权。request_data/compute 不继承只读豁免，lookup 的 read-only/never 不放宽。守卫区分该审批冲突与目录服务故障；仍停下记费用，不自动重试。

增量回归47项通过（MCP HTTP鉴权/工具清单、失败结算与并行、查找/委托/能力匹配及提供方契约），Ruff/Pyright通过。两个契约测试最初因独立子仓worktree不是父子目录布局而失败，随后以精确父仓锁文件、提供方schema及候选测试在临时标准目录复跑，2 passed；没有改契约以求通过。

真实 Codex 0.155.1 离线协议探针新增实际工具调用（不止 tools/list）：旧无注解声明在 never 下拒绝且MCP零执行；生产 find_expert 声明在同策略下真实执行；request_data 仍拒绝且零执行。连同前一轮搜索范围6项共9项通过；本地模拟HTTP模型与stdio MCP，不用真实Key、不产生付费模型或Wind调用。运行：`PYTHONPATH=. uv run --locked python tests/probe_native_search_scope.py /path/to/codex-0.155.1`（app目录）。

发布仍交Qwen，同批backend/API/runner和既有登录修复web；不重孵化专家、不复活已停止任务。发布后须查真实 tools/list 与新线程mcpServerStatus/list的find_expert注解；旧线程缓存须在无活动turn时按原正式协议刷新。真实模型匹配尚未重试，不宣称线上成功。

## 2026-11-04 #71 后匹配结果读取失败

基线321b779（#71）。现场43431b70与decbc27b均已SUCCEEDED，但结果接口409：前者把scope/v1声明正文中的F4当作public_capabilities的ID（实际数组为空），后者question_quote含自行添加的省略号。859b20aa的none结果能正常读取。目录工具已实际执行；不是本轮登录故障或此前MCP审批阻断。

缺陷是完成时只验JSON/必需键，到GET及委托才验匹配收据。现在完成前复用同一原题/目录快照/声明/费用覆盖校验；具体失败理由送原一次返工，再不通过按原流程等待处理，预算不扩大。提示词明确scope/v1用null能力ID、引文只能是连续原文，不能把正文编号当结构化ID。旧SUCCEEDED但证据不合法的轮次GET返回明确error，不给outcome/picks、不修改历史、不再返回笼统409；委托端仍拒绝。

52项通过：新增5项隔离DB回归、既有动态匹配到执行/固定查找/领域匹配共34项，通用运行器与查找守卫18项。覆盖首次错误→说明返工→成功、两次失败无成果且无无限重试、旧错误结果可重复只读查看且仍不能委托。Ruff/Pyright/diff检查通过。使用模拟模型，不产生付费调用；未改生产任务、费用或专家版本。发版交Qwen，仅backend/API/runner；#71 web/admin沿用。待发布后的真实模型匹配核验，不宣称语义覆盖已通过。

开发分支同步时保留未发布v3/v4的parse_match及按任务版本核契约；新增完成前检查仅作用v2，未把新架构带入#71发布补丁。冲突解决后新增验收及匹配领域25项通过，Ruff/Pyright通过；原K29未提交文件保留。

## 2026-11-04 正式分析空参数计算循环止损

现场委托14a8eba0-cda1-4531-9e4d-60f59c26483b，reconcile从04:58开始。14次compute全部arguments={}，返回task not found，每次10–123ms；此前query_facts/read_input/search_evidence成功。最终72项（16资产负债、16利润、16现金、24衔接）全列missing，未算成功；流程随后到indicators，仍有空参失败。不能因步骤打勾就宣称勾稽通过。

用户明确批准停止后，05:17:28经正式cancel_task应用服务记录取消并排turn.interrupt。只读确认任务CANCELLED、当前attempt CANCELLED、turn interrupted、active_attempt_id=null、预留0，累计估算24.142204元。scope/inventory/reconcile/series交回物及钉住数据保留，无任务复活、重测或费用改写。

新增compute缺task的明确错误（在任务查询前返回，不再误报任务不存在）；说明单项与批量均需task。正式执行的compute连续3次缺task且失败则经原interrupt/确认机制停止，保留费用；中断未确认沿用turn_open对账，不能放新执行。只计本工作台compute真实失败且去重通知，正确传参、正常批量、其它读工具和普通聊天不受该阈值影响。不改变财务公式、容差或作者验收。

增量48项通过：缺参提示、守卫去重/恢复、正式运行确认与未确认中断、原批量计算、MCP鉴权、数据规划和查找守卫、顾问回归。Ruff/Pyright通过。原匹配修复52项另计，不声称全部不重复。

根因边界：Codex0.155.1离线捕获的供应商请求schema仍有task/expression/inputs/batch，不能据此断言Kimi或转译网关为何持续发{}。未凭猜测删除oneOf或放宽计算验收；本增量是明确报错及限制浪费，不宣称已根治空参生成。2023H1补提被拒也尚未定位，不能宣称后续预测窗口数据齐全。下一步需在发布后按用户授权验证真实模型的完整计算参数及compute_id，不自动新增付费调用。

## 2026-11-04 compute 参数定义兼容修复与真实供应商对照

基线1619028（同一待发布候选）。用户授权仅虚拟数据/模拟工具、真实模型总额最多2元，未重开已取消财务委托。现场Codex rollout中17次compute的function_call.arguments已是{}；直接请求同一kimi-k3 Responses接口也复现旧schema单笔{}，SSE delta/done均为空，排除必须由Codex或工作台转发丢参才能解释该现象。旧schema后一次更换合成ref格式也成功，不能宣称必现或把所有历史问题归于此。

旧oneOf/not分支只列required，Codex0.155.1转成properties={}的分支并丢not。只去掉oneOf仍复现仅task、漏公式与inputs；根部不带object的anyOf会被供应商400拒绝。最终保留根部object/字段/必需task，单笔与批量改为自包含anyOf分支，各自明确字段、必需项与additionalProperties=false。工具名、单笔/批量入参形状、混用拒绝、所有权、公式、证据、容差均不变，不改专家包。参考Moonshot官方MFJS规范（支持anyOf）与真实接口实测，不将模型/供应商内部转译的具体责任作未经证实的判断。

工程验证：真实Codex0.155.1离线探针新增计算工具出站schema校验、单笔与批量完整参数到stdio MCP、结果回模型；在隔离临时目录的full-access策略验证compute（不把compute标成readOnly，不改变生产权限），原read-only/never三项拒绝/放行测试保留。12个PASS检查均通过。最终精确出站schema真实模型2次单笔、1次批量全部完整；此前等价完整分支两种调用也通过。最终三份模型参数原样交真实compute应用服务＋内存事实/事件替身，均得350.35与有效c编号，无生产记录写入。

回归168 passed（56.19s）：compute_batch、compute_batch_db、knowbot_compute、workbench_mcp_routes、compute_progress_guard_db、method_evidence_delivery_db、workbench_advisor_db；Ruff/Pyright/diff通过。新负例覆盖{}、仅task、空batch仍不合法且零计算记录，既有混合模式拒绝与批量原子性仍过。测试最初从父目录误跑uv提示缺项目，已在app目录正式运行，不掩盖该执行错误。

隔离对照共14次HTTP200、6次参数校验HTTP400（未返回用量），所有真实请求max_output_tokens=768、无自动重试。按返回usage和应用价目表估算0.461396元，额外保守计缓存写入0.492116元，低于2元；供应商账单为准。证据在sunmoonun/docs/总纲/发版/证据/2026-11-04-compute参数对照.json，保留请求、仅工具参数SSE事件、usage与虚拟服务回放；无Key、业务数据和模型推理正文。

发布仍交Qwen，统一交接《2026-11-04-AI选专家结果验收修复交接.md》。发布后旧线程工具缓存要在无活动turn时刷新，真实tools/list和mcpServerStatus/list需核完整anyOf字段。新财务任务须用户显式发起；已取消14a8eba0不恢复。该证据不等于完整财务委托验收，2023H1缺项补提被拒仍另查。

## 2026-11-04 范围说明中逗号后否定措辞误判

基于f240415（#72候选），仅修改确定性no_positioning_advice判定。现场733b21b5的scope首轮37919c3a验收失败；用户原文r8.scope_reason为“共同前提明确：最终交付财务分析报告.md，含出处与局限，不含评级、目标价或买卖建议”。原规则只允许完整否定声明从句首/句号/分号等处开始，逗号后的明确否定未豁免，误命中目标价。用原句离线回放旧版False/命中目标价，新版True，原JSON与正文均未修改。

修复允许完整否定声明从中英文逗号后的分句开始；结尾约束不放宽，不把“……不提供目标价，但可参考1800元”当作免责声明。仍逐文本叶子检查，不能遮住同句其余内容、其他行/其他字段的实际建议；不删除规则、不改专家包、不把失败历史改成通过。新增10项正反例；相关148项回归通过（acceptance_disclaimers/workbench_acceptance/acceptance_extensions/acceptance_json_feedback/acceptance_precision/advisor_acceptance_prompt），Ruff/Pyright/diff通过，无模型费用。

只读核验时scope第二次840ba372已COMPLETED，任务进入inventory；未替用户重试、取消或改状态。发布交Qwen另排增量，不能覆盖#72在建钉版，不能滚动中断当前付费任务。统一交接索引链接到《2026-11-04-范围说明否定措辞误判修复交接.md》。本确定性规则不是完整自然语言合规判定，未宣称覆盖任意否定句式。

## 2026-11-04 勾稽取数收敛与数据步骤搜索范围

基线 a9e1618（含范围否定措辞修复）。73081dc1首轮勾稽238秒，query_facts完成15/失败3，compute零调用，最终agentMessage仅“标签”，Codex task_complete标为completed；最后请求输入208250tokens，输出1687tokens。只读证据只能证明缺项反复检索、上下文累积与异常终止并存，不能证明token上限截断或具体供应商转译根因。没有伪造JSON、放宽验收或增加重试。

数据工具已声明的专家执行步骤继续关闭native web_search（复用钉版Codex已验证的idle unsubscribe/resume机制），避免数据规划后恢复native搜索，导致模型拿网页找库内fact ref；普通聊天/无数据工具专家保持原设置。compose补充复用已返回引用、raw按label查询、完整数据先执行compute.batch、缺项按作者既定规则处理或提问；不能替换原式、改变来源/覆盖/容差。提示指导不等于硬性保证模型收敛，没有宣称根治提前交回。

验证：78项 prompt/runtime-tools/compute/免责声明回归；25项真实WebSocket+隔离DB（data-planning runner、advisor、compute_batch）；Ruff/Pyright无错。未付费调用模型。相关knowfeed修复与原件回放见 fix/statement-label-continuations。新代码未部署，旧任务不重启/不改记录。后续发布需等所有活动turn结束；处理旧读法后用户发起任务，核真正compute回执及最终JSON，不把离线通过当财务验收通过。

# 2026-11-04：正式步骤无进展保护（独立发布候选）

基线 8a662f1（已发布 #73），分支 fix/step-idle-guard；K29 未提交保持原工位不动。
正式固定/动态步骤共用 StepProgressGuard：180秒无新成功工具结果、同参数同结果累计3次、
工具执行300秒、步骤总900秒分别记原因。agentMessage/reasoning只记活动，不延长时限；
started/completed分开去重，工具运行不计模型空闲，等人从两种时钟扣除。
确认 turn 停止后结算真实费用并进 WAITING(INPUT)，不走专家验收失败的自动返工；
停止未确认保留 turn_open，不提供重做。 data-plan/找专家/动态选方法沿用原控制。
绑定固定和动态尝试到 RUNNING/started_at/runtime_turn_id，turn_ids仍只用于费用结算；
取消后的迟到费用回归只记一次。没有新增迁移、环境变量或专家包变化。
同时补齐既有0038迁移的静态测试清单遗漏（该迁移早已存在并发布，不是本次新增）。
规则：C-D11账房真源、C-A9不新增模型客户、C-A10预算不追加、C-R10离线通过不当线上通过。
最终合并回归84 passed（含真实隔离PostgreSQL/WebSocket）；日志 /tmp/luna-step-guard-final-tests.log；Ruff、Pyright通过。发布授权为所有者本轮
“你修好直接发版”，使用既有Make/Jenkins；不启动真实模型委托。

## 2026-11-04 定数据工具指令前置（#74 后继，独立候选）

基线 b9b937a（#74），分支 fix/data-plan-discovery-order。审查原 240/2400 字截断候选后不采用：任意前缀不是摘要，会删除作者输入要求；无条件首动作调用工具亦与范围门冲突。本次保留 #74 的 1200/12000 投影上限，将作用域检查之后、方法参考之前放置条件化发现指令，仅需数据且问题在范围内才查主体/供应商并提交 request_data。保留供应商 schema、PDF 原件、table 事实、非公司题/无数据题及作者显式预测要求等边界。没有修改专家包、时限、模型或 effort，没有混入主工位 K29。

新增超过240字之后的数据要求保留及工具顺序/数据边界回归。组合回归初跑88 passed/4 failed，四项均为旧提示固定措辞断言；恢复原“empty inputs list is normal”后 scope+flow 重跑16 passed，未放宽测试。其余规划守卫、runner、schema、专家数据及找专家用例均在初跑中通过。Ruff/Pyright/diff 检查通过，无真实模型调用。

证据边界：现场 15KB 用户提示与180秒零完成工具并存，不能证明长度是唯一原因。Kimi K3 官方说明缺省 effort=max，现场请求未指定；隔离低 effort 亦有超时，故本版不擅自改 effort，不宣称根治供应商或网络问题。只发布提示结构修正，不自动新开付费委托、不更改旧失败/费用记录。

## 2026-11-04 定数据时限改为六分钟（所有者指示）

#76 后任务 b435c1e9 使用了新提示，02:07:31Z 开始；02:08:12Z 收到供应商 engine overloaded 自动重连；02:08:17Z 开始reasoning，02:10:26Z完成；02:10:27Z find_subject/discover_source_tools 均成功，02:10:31Z达到180秒总限被确认中断。报告7360输出token，其中7151推理token；记录费用0.776932元。不能解释为工具失败或新提示未上线。只查看事件类型/计数与公开工具结果，不读取/保存推理内容。

所有者询问“那时间改长点不行吗”，已明确告知并采用6分钟：DATA_PLAN_SECONDS=6*60；提示分钟数由常量生成，避免代码/文案漂移。仅定数据；找专家120秒、正式步骤无进展180秒/总限900秒、费用/重复工具守卫均不变。没有修改模型effort，没有重启已暂停的委托，旧提示/历史费用原样保留。后续若重做，按新时限运行，不自动追加预算或重试。

回归覆盖 data_planning_guard、真实WebSocket/隔离DB的data_planning_runner和flow；结果见本轮发版回执。C-D11保持任务真源；C-A9继续仅app-server驱动；C-A10不加预算；C-R10不把时限调整等同财务验收或性能保证。

## 2026-11-04 确认与定数据结束竞态、已有资料续跑

在d4b4e2c六分钟候选上补修。生产委托b435c1e9用户于02:16:59Z确认，模型02:17:04Z结束，旧Advisor把已确认的waiting覆盖ready；Task仍WAITING(DATA)，poller不再领走。confirmation63b51968、demand410d2726仍存在，非用户没确认。

- Advisor仅把自己仍拥有的planning改ready，保留确认方waiting。
- 原轮询/回调领用时，只对ready+WAITING(DATA)+confirmation+demand+next_check+同phase supplying请求的历史竞态恢复waiting；不复活终态，不推断未确认请求。
- 供给单尚在fetching也按已确认范围重解析；全部条目与读法resolved且可续签才钉住、正常resume_data，并撤掉本需求单（共享作业不取消）。未齐继续等待，不默认缺数照做。保留先前供给快照供审计，清除已无意义的处理中显示。
- 无生产DB直改、无代点确认、无新付费测试。既有确认恢复执行仍遵守原费用和任务规则。

验证：68 passed（隔离PostgreSQL+HTTP/WebSocket，/tmp/luna-confirmation-race-tests.log），包括确认先于turn完成、旧竞态恢复、库齐但fetch未结束、未齐仍等、重复poll不重复授权、缺确认/终态不恢复；Ruff通过，Pyright0errors。初轮65passed/1failed源于旧替身在fetching时已经返回库齐，修正该测试的到货时序后全部通过。待既有Jenkins与knowbase匹配修复同版发布并核真实23项续签/钉住/任务续跑。


## 2026-11-04 按所有者指令取消专家执行时限

隔离基线 205b2a1；用户明确要求“取消所有时间限制”。专家查找、定数据、方法规划、正式步骤默认 duration=None；正式步骤无进展180s与工具5min守卫取消时间判定。保留重复结果/空参数/工具越界保护、原预算、用户停止及费用记账。常规网络连接/RPC超时、身份令牌有效期和作者测试所明确选择的整轮测试参数属于其它边界，本提交未改；没有自动重试失败任务或代确认数据。

真实现场 ad01a782 勾稽三次（后续共五次）step_progress/no_progress，Codex HTTP200及reasoning item start后，180s内无完成工具/交回物被interrupt；不是财务验收失败。数据摸底已成功9次MCP调用，工具结果累计约344625字符；提示约34724字符。不能仅凭长度断言模型根因，也不把取消时间上限当作财务分析已通过。

验证：41项通过（真实WebSocket/隔离Postgres含三个阶段无时限且可手动中断、费用留存、原有显式有限时测试、查找预算、循环保护、取消迟到费用）；ruff通过，pyright零错误。新增生产迁移：无。固定专家包及方法不变，无新模型付费测试。

## 2026-11-06 工位合并与测试入口隔离

合入 release #83 与未发布声明匹配/方法目录开发，默认执行时限继续为 None，保留预算与无效调用守卫。恢复并补齐 K29：浏览器工作台不列测试项目、会话、任务及交互，内部 knowlab 执行读取保持可用；正式测试绑定仍检查作者和任务来源。新增 20261106_0039 迁移接 0038，补 origin 枚举、历史回填与降级；独占测试项目隐藏，混合项目保留。没有修改线上数据库或触发发布。

隔离 PostgreSQL 回归覆盖新/旧绑定、跨用户拒绝、正常会话可见、迁移降级再升级及混合项目保留。旧未提交六分钟时限与旧定数据提示被 #83 正式实现取代，原补丁保留在本地备份，不回灌。开发合并结果需新发布迁移后才生效。
