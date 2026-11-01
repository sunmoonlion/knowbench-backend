# 工具回传故障与步骤推进修复（2026-11-02）

基线：`6032cbd940e96ed36f18eefacee468537591ec3c`；分支：`fix/tool-roundtrip`。
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

结论边界：目前证据支持“间歇性/条件性 Responses 工具回传不兼容”，还不能确定触发条件或供应商内部根因；不能宣布永久修复，也不能用单次 200 证明完整财务提采闭环成功。未添加 ID 猜测、隐式重试或永久协议代理。

## 配置问题

qwen/sunmoonun 的未提交 entrypoint 改动曾把 `web_search_mode = "never"` 写入 `[features]`。实际 0.155.1 会报 `invalid type: string "never", expected a boolean in features`。线上实际 config 没有这一项，且 REWRITE_CONFIG=0，因此此前“配置与环境一致且已根治”的说法不成立。

已在获得目录写入权限后仅删除这条无效生成行，保留其余未提交改动。这个改动在 qwen 工作区，不属于本后端分支提交。有效的搜索模式是顶层 `web_search` 的 disabled/cached/live；本次未全局关闭搜索。`standalone_web_search=false` 本身不能证明所有搜索入口关闭，更不能修复此次调用 ID 错误。

## 验证与待办

回归覆盖：错误只影响匹配会话、可重试错误不污染成功、终态错误保留、中断显式、定数据失败/超时/中断即使带进度文字或部分需求也不推进。使用独立数据库 luna_roundtrip_tests；每个测试使用隔离 schema。

命令：`python3 /tmp/luna_roundtrip_db_tests.py tests/test_workbench_turn_outcome.py tests/test_workbench_expert_data_db.py -q --tb=short`；该本地包装器仅从现有测试容器读取连接配置供进程使用，不输出凭据。

已运行 ruff format、ruff check 和 git diff --check。两组回归结果见交回提交说明。

剩余：发布前确认部署版本与回退入口；发布后通过实际工作台验证失败不误推进与 find_subject → request_data → 用户确认的闭环；若再次出现 400，保留脱敏的同一次失败请求/响应配对并与模型供应商核对。没有未经确认地派发真实采集任务。
