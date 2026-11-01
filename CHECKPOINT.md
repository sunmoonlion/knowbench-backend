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
