# Agent 会话、缓存与服务可靠性

## 请求上下文

每次模型请求都由两部分组成：

```text
静态前缀 = system prompt + 本轮开始时冻结、按名称排序的工具 schema
动态上下文 = 活动会话历史 + 当前用户消息 + Skills 路由 + assistant/tool 轨迹
```

静态前缀由 `backend/agent_runtime/prefix.py` 计算 `prefix_hash`。同一项目缓存目录再次看到相同 hash 时记录 `prefix_cache_hit=true`；如果模型服务返回 `prompt_cache_hit_tokens`，也会原样记录到 flags/run logs。这个本地命中字段不冒充服务商真实 KV cache 命中。

动态消息不参与 system prompt 拼接。会话恢复时只从活动消息头遍历动态消息，system prompt 每次从当前代码重新构造，因此历史增长、Skills 内容变化和工具结果不会破坏静态前缀。

## 会话记忆与回滚

主页的历史归属和 Agent 上下文是两个概念：浏览器保留稳定的 `session_id`
用于 `/v1/history`，每次新的主页提交都生成独立 `context_id`。因此同一用户的历史
记录仍会出现在一起，但一次新问题不会读取上一条主页问题的动态消息；只有同一个
run 的 ReAct 继续和用户补充回答会复用该 context。

`SessionStore` 将每个会话写入 `.cache/sessions/<session_id>.json`：

- 消息记录 append-only，带 `message_id`、`parent_id`、`run_id` 和 OpenAI 消息字段；
- `active_head_id` 是下一轮可见历史的唯一游标；
- 回滚只把活动头移到目标消息的父节点，旧分支仍在文件中；
- 新消息从新活动头继续追加，形成新分支；
- 文件通过临时文件 + `os.replace` 原子替换，不保存 API Key 或请求头。

## ReAct 计划与等待用户

Agent 通过受控的 `set_plan` 工具发布面向用户的步骤，通过 `ask_user` 工具发布最多
8 个结构化问题。前端逐题展示选项和自定义输入，允许返回上一题修改，全部完成后以
`POST /v1/runs/{run_id}/input` 恢复同一个 run。等待期间 RunStore 保存去敏后的
checkpoint，状态为 `waiting_user`；SSE 会回放 `plan_update`、`tool_started`、
`tool_completed` 和 `input_required`，页面只展示这些可验证事件，不展示隐藏思维链。

`set_plan` 和 `ask_user` 不访问数据。真实数据工具仍由模型在 ReAct 循环中选择，
工具开始前后分别发出状态事件，因此页面不会再把固定的四步流程冒充 Agent 计划。

## 空结果与可靠性

领域工具统一返回 `result_status`、`reason_code`、`observed`、`filters_applied` 和
`relaxation_candidates`。城市、生产年份、最小样本门槛属于默认可尝试的软条件；预算、
指定车系和能源类型不允许静默放宽。连续多次仍为空时，运行会停止继续放宽并要求用户
确认，或者生成 `no_data` 报告。没有形成可比样本时，可靠性 `sampleSize` 为 `null`
并显示“未形成可比样本”，不会把命中数 0 伪装成统计样本量 0。

CLI 可这样复用上下文：

```powershell
python backend/run_demo.py --session-id demo-1 "我的预算是15万"
python backend/run_demo.py --session-id demo-1 "继续帮我选车"
```

## 断线重连

`backend/service.py` 提供可选 FastAPI 服务：

- `POST /v1/runs`：创建后台 Agent run；
- `GET /v1/runs/{run_id}`：读取状态和最终结果；
- `GET /v1/runs/{run_id}/events?after_seq=N`：SSE 回放；也支持 `Last-Event-ID`；
- `GET /v1/sessions/{session_id}/messages`：读取当前活动分支。

客户端断线不会取消后台 run。重新连接时先回放序号大于游标的事件，直到 `done` 或 `error`。失败 run 可通过 `POST /v1/runs/{run_id}/retry` 创建新 run，原用户消息不会再次追加。

## Run 生命周期与提交边界

HTTP run 的生命周期以 SQLite 快照为准。公开快照带 `protocol_version=2`、`attempt`、`state_revision` 和 `last_event_seq`：语义状态变化推进 `state_revision`，新增事件推进 `last_event_seq`，租约执行片段推进 `attempt`。心跳不会伪装成用户可见状态变化。

`waiting_user` 是可恢复暂停。pending input、Agent checkpoint、runtime、resume revision 和 `input_required` 事件在同一事务提交；回答被接受时，`ask_user` 的 tool 消息、批次游标和新 checkpoint 一起提交。`AgentState.from_checkpoint` 会拒绝问题、assistant tool call、已完成 tool 消息和批次游标彼此不一致的快照。

成功结果先经过 `CompletionPolicy`：回答非空、证据仍匹配当前约束、计划已收口且报告投影成功后，才进入完成事务。SQLite 的 `complete_run` 再用 run lease 和 context revision fencing 原子提交会话增量、报告、runtime、终态和终态事件。模型的文本预览事件不能单独驱动 `done`。模型响应边界保留 `finish_reason`、provider request ID 和模型耗时；截断或空响应不会被当作完整答案。

前端由 `analysisAdapter.followRun` 统一处理创建、回答、纠正、恢复和重试的事件流，`runReducer` 按事件序号更新运行状态；续订游标只在事件应用后推进。POST 带 `AbortSignal`，组件卸载或新一代运行开始会停止旧请求，generation 检查阻止迟到回调覆盖新状态。当前 SSE 使用有界分页和低频 fallback 轮询，SQLite 读取在 worker thread 执行；跨进程提交通知、真实模型首 token 指标和生产 P95 尚未作为发布 SLO 验证。

启动：

```powershell
python -m pip install -r requirements.txt
uvicorn backend.service:create_app --factory --host 127.0.0.1 --port 8000
```

## Skills

项目级 Skills 放在：

```text
.agent/skills/<name>/SKILL.md
```

运行开始时只扫描名称和摘要，并把摘要作为动态尾部消息；模型需要完整步骤时调用固定 schema 的 `load_skill(name)`。读取路径、名称和正文长度均由 Python 校验，不能传任意路径。

## MCP

`MCPManager` 是显式注入式适配层，不自动启动进程、不访问网络。宿主注册 provider 和工具 allowlist 后，运行开始时生成稳定排序的 MCP schema 快照；快照会参与 `prefix_hash`，中途不变更工具集。单个源具备按幂等性区分的超时、有限重试、连续失败熔断和 `close()` 清理。非幂等调用超时返回 `unknown_outcome`，不会自动重放；每个调用携带 `run_id + tool_call_id` 的哈希幂等键。默认二手车 Agent 不注册任何 MCP 工具，因此原有五个本地领域工具边界不变。

## SQLite 迁移与部署

生产服务把 `.cache/runtime.sqlite3` 放在持久卷中，owner cookie 使用部署密钥签名。容器入口在第一次启动时以 `LEGACY_OWNER_ID` 执行幂等 JSON 导入；导入成功后写入 `.json-runtime-imported` 标记。迁移旧 JSON 时应先停止旧 writer、备份 `runs/` 和 `sessions/`，再执行：

```powershell
python -m backend.migrations.import_json_runtime `
  --runs .cache/runs --sessions .cache/sessions `
  --database .cache/runtime.sqlite3 --legacy-owner-id legacy-import
```

检查导入数量、最大事件序号、会话活动头和重复执行结果后再启动服务。`/health` 只表示进程存活；`/health/ready` 检查 SQLite 与 scheduler 可接收任务；`/health/runtime` 返回受控的 scheduler 统计，不包含租约 token 或 worker 标识。
