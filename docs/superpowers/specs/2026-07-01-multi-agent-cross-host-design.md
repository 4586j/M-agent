# 多 Agent 跨主机协同设计（V2）

> 在 V1 单机形态基础上，把 executor 派发远程化，实现多主机 Claude Code 协同。security-guard 留大脑本地，纯 HTTP 协议贯穿。

---

## 1 背景与目标

V1 多 agent 编排框架已实现（大脑 skill + executor/security-guard 子 agent，单机单进程）。V1 设计文档第 7 节预留了跨主机扩展点：派发、记忆、规则三类关注点的「做什么 vs 在哪做」分开描述。

V2 目标：兑现该扩展点，把 executor 派发从「本地 Agent 工具」换成「远程 HTTP 调 worker」，实现多台主机的 Claude Code 协同工作。security-guard 因只读、轻量、需低延迟响应大脑，留大脑本地不跨主机。

### 已对齐前提

| 决策点 | 选定 |
|--------|------|
| 主机角色拓扑 | 1 大脑 + N worker，大脑主动派发 |
| 大脑↔worker 通信 | 常驻 HTTP 服务 + Agent SDK 程序化执行 |
| worker 执行体 | Claude Agent SDK 起 executor 会话 |
| 角色部署 | worker 按角色专化（V2 只做 executor-worker） |
| security-guard 位置 | 留大脑本地（不跨主机） |
| 记忆共享 | 集中存大脑 + 接口抽象（将来可换共享存储） |
| 文件协同 | worker 本地落盘 + 按需 HTTP 回传 |
| worker 发现 | 动态注册 + 心跳 |
| 故障处理 | 任务持久化 + 可恢复 |
| 调度通道 | 纯 HTTP（一套协议，不用 MCP） |
| 落地形态 | 方案 A：Dispatcher 接口抽象，V1 单机保留、V2 远程叠加 |

---

## 2 整体架构与角色拓扑

```
        ┌──────────────── 大脑主机 ────────────────┐
        │                                          │
        │  主会话 (orchestrator skill)             │
        │    │ 七阶段工作流（不变）                  │
        │    │                                      │
        │    ├─> Dispatcher 接口（新增抽象）         │
        │    │     ├─ LocalDispatcher   (V1 保留)   │
        │    │     └─ RemoteDispatcher  (V2 新增)   │
        │    │                                      │
        │    ├─> security-guard (本地子 agent, 不变) │
        │    │                                      │
        │    ├─> WorkerRegistry (注册+心跳+调度)     │
        │    ├─> TaskStore (任务持久化)              │
        │    └─> MemoryStore (集中存, 接口抽象)      │
        │                                          │
        └──────────────┬───────────────────────────┘
                       │ HTTP
            ┌──────────┴──────────┐
            ▼                     ▼
     ┌─────────────┐       ┌─────────────┐
     │ executor-   │       │ executor-   │   N 个，按角色专化
     │ worker A    │       │ worker B    │   （V2 只做 executor）
     │ (HTTP 服务) │       │ (HTTP 服务) │
     │  Agent SDK  │       │  Agent SDK  │
     └─────────────┘       └─────────────┘
```

### 关键边界（呼应 V1 第 7 节预留扩展点）

| 关注点 | V1 实现 | V2 实现（做什么不变，在哪做换） |
|--------|---------|-------------------------------|
| 派发 executor | 本地 Agent 工具 | Dispatcher 接口；RemoteDispatcher 走 HTTP 到 worker |
| 派发 security-guard | 本地 Agent 工具 | 不变（留大脑本地） |
| 七阶段流程 | SKILL.md | 不变 |
| 记忆访问 | 本地文件 | MemoryStore 接口；V2 落地仍集中存大脑，但走接口 |
| 安全规则 | 本地 YAML | 不变（security-guard 在本地读） |

### 核心设计点

- **唯一新增抽象是 Dispatcher 接口**：大脑 SKILL.md 描述「派发 executor」动作不变，底层从 LocalDispatcher 切到 RemoteDispatcher。V1 单机形态完全保留可用。
- **security-guard 不跨主机**：只读、轻量、需低延迟响应大脑七阶段流程，留本地。V2 范围收窄到「只把 executor 派发远程化」。
- **worker 按角色专化**：每个 worker 只跑 executor（V2 范围）。
- **纯 HTTP 一套协议**：任务派发、注册、心跳、文件回传全是 HTTP 端点。

---

## 3 大脑侧组件

4 个组件，跑在大脑主机，单一职责、可独立理解。

### 3.1 Dispatcher 接口（派发抽象）

```
Dispatcher（接口）
  ├─ LocalDispatcher    V1 保留：本地 Agent 工具派发
  └─ RemoteDispatcher   V2 新增：HTTP 派发到 worker
```

**接口契约**：`dispatch(role, prompt, schema, model) -> DispatchResult`
- `role`：`"executor"`（V2 范围）；security-guard 不走 Dispatcher，直接本地派发
- `schema`：结构化输出约束（executor 一般无）
- 返回 `DispatchResult`：`{task_id, status, output, artifacts, error}`

大脑 SKILL.md 里「派发 executor」只调 `Dispatcher.dispatch(...)`，不感知本地/远程。配置决定用哪个实现。

### 3.2 WorkerRegistry（注册 + 心跳 + 调度选择）

| 能力 | 说明 |
|------|------|
| 注册 | worker 启动时 POST `/register`，带 `{worker_id, role, host, port, capabilities}` |
| 心跳 | worker 定期 POST `/heartbeat`；超过 45s（3 周期）未心跳 → 标记失联 |
| 选择 | `select_worker(role) -> worker`：从活动 executor-worker 选一个（V2 轮询，预留按负载） |
| 剔除 | 失联 worker 移出活动列表，其未完成任务交 TaskStore 处理 |

### 3.3 TaskStore（任务持久化 + 可恢复）

每个派发任务带 `task_id`，状态持久化到磁盘 JSON 文件（V2 不引入数据库）。

```
TaskRecord: task_id, role, prompt, worker_id, status, created_at, updated_at,
            output, artifacts, error
status: pending | running | done | failed | lost
```

- 派发前写 `pending`，worker 接受后 `running`，完成回传 `done`/`failed`
- 心跳超时且任务 `running` → 标 `lost`，大脑可重派（新 task_id 关联原任务）
- worker 恢复后通过 `/tasks?worker_id=` 查询自己未完成任务接续

### 3.4 MemoryStore（记忆访问抽象）

把记忆访问从「直接读写本地文件」抽象成接口。V2 落地仍集中存大脑本地，但走接口。

```
MemoryStore（接口）
  ├─ 核心：read_core / write_core / delete_core（走共识流程，不变）
  └─ 上下文：read_context(task_id) / write_context(task_id, key, val)
FileMemoryStore（V2 实现）：本地文件（内置 memory + workspace context 目录）
```

共识流程不变（三方投票），写入动作经 `MemoryStore.write_core`。worker 不直接访问记忆——大脑派发时把相关上下文记忆随 prompt 下发，worker 产出记忆候选回传大脑走共识。

### 组件依赖

- SKILL.md → Dispatcher + 本地 security-guard 派发
- RemoteDispatcher → WorkerRegistry + TaskStore
- 共识记忆 → MemoryStore

---

## 4 worker 侧组件

每个 executor-worker 是一台主机上的常驻 HTTP 服务，内部用 Agent SDK 执行。单一职责、自包含。

### 4.1 Worker HTTP 服务（FastAPI）

| 端点 | 方向 | 职责 |
|------|------|------|
| `POST /register` | worker→大脑 | 启动时注册 `{worker_id, role="executor", host, port, capabilities}` |
| `POST /heartbeat` | worker→大脑 | 定期（15s）报活，带负载 |
| `POST /task` | 大脑→worker | 接受任务 `{task_id, prompt, context_memory, model?}`；立即返回 accepted，异步执行 |
| `GET /task/{id}` | 大脑→worker | 查询任务状态与结果 |
| `GET /artifact/{id}` | 大脑→worker | 拉取产出文件（按需回传） |
| `POST /task/{id}/cancel` | 大脑→worker | 取消任务（V2 最小实现，标记意图） |

任务执行**异步**：`POST /task` 立即返回 `accepted`，大脑通过 `GET /task/{id}` 轮询。避免 HTTP 长连接阻塞。

### 4.2 AgentRunner（执行体）

用 Claude Agent SDK 在 worker 本地启动 executor 会话执行任务 prompt。

- 复用 V1 `.claude/agents/executor.md` 角色定义（工具集、system prompt、model），worker 启动时加载传给 SDK
- 接收大脑下发的 `prompt` + `context_memory`
- 产出：代码/文件落盘到本地 `task-{id}/`，结论作为输出文本回传
- 不直接访问大脑记忆；记忆候选随结果回传，由大脑走共识

### 4.3 本地工作区与文件回传

- 每个 task 独立目录 `task-{id}/`，产出文件落此
- 结果回传大脑时只回传**元信息**（文件路径清单 + 结论文本），不主动传文件
- 大脑需要实际文件时调 `GET /artifact/{id}` 按需拉取（安全关卡②检验代码时触发）

### 4.4 worker 配置

- `brain_url`：大脑 HTTP 地址
- `worker_id`：本机标识
- `role`：固定 `executor`（V2）
- `executor_def_path`：executor.md 路径
- `work_dir`：本地工作区根

---

## 5 数据流与协议

### 5.1 端到端时序

```
用户指令
  ▼
大脑七阶段 ①意图澄清 → ②边界探索 → ③prompt优化 → ④任务拆解
  ▼ ⑤安全关卡①（指令）：security-guard 本地派发 → pass
  ▼ ⑥执行调度
  ├─ TaskStore.write(pending)
  ├─ WorkerRegistry.select_worker(executor) → worker-A
  ├─ RemoteDispatcher.dispatch → POST worker-A /task
  │      └─ worker-A: AgentRunner 起 executor 会话，产出到 task-xxx/
  ▼ 轮询 GET /task/xxx → done → TaskStore.update(done, output, artifacts)
  ▼ ⑦结果审核 + 安全关卡②（产物/代码）
  │  需检验代码 → GET /artifact/xxx 拉文件 → security-guard 本地检验
  │  pass + 达意
  ▼ 共识记忆提炼 → 三方投票 → MemoryStore.write_core
  ▼ 回报用户
```

### 5.2 任务协议

**派发 `POST /task`**
```json
{
  "task_id": "task-20260701-001",
  "prompt": "分析 workspace/test.csv，产出报告",
  "context_memory": "相关上下文记忆文本（大脑注入）",
  "model": "sonnet"
}
```
worker 立即返回：`{ "task_id": "...", "status": "accepted" }`

**查询 `GET /task/{id}`**
```json
{
  "task_id": "task-20260701-001",
  "status": "done",
  "output": "分析结论文本...",
  "artifacts": [{"path": "task-xxx/report.md", "size": 2048}],
  "memory_candidates": [],
  "error": null
}
```

**拉文件 `GET /artifact/{id}?path=task-xxx/report.md`** → 文件流

### 5.3 注册与心跳协议

**注册 `POST {brain}/register`**
```json
{ "worker_id": "worker-A", "role": "executor",
  "host": "192.168.1.10", "port": 8000,
  "capabilities": {"model": "sonnet"} }
```

**心跳 `POST {brain}/heartbeat`**
```json
{ "worker_id": "worker-A", "load": 1, "ts": "..." }
```
超过 45s 未收到 → 标失联。

### 5.4 故障与恢复流程

```
worker-A 心跳超时
  ├─ WorkerRegistry 剔除 worker-A
  ├─ TaskStore 查 worker-A 的 running 任务 → 标 lost
  └─ 大脑对 lost 任务：可重派（新 task_id，TaskStore 关联原任务）

worker-A 恢复
  ├─ POST /register 重新注册
  └─ GET {brain}/tasks?worker_id=worker-A
       └─ 拿回自己未完成的 running/lost 任务
            ├─ 本地 task-xxx/ 还在 → 接续
            └─ 本地 task-xxx/ 丢失 → 报告 failed，大脑重派
```

### 5.5 错误处理边界

| 场景 | 处理 |
|------|------|
| 无活动 executor-worker | RemoteDispatcher 报错，大脑告知用户「无可用 worker」 |
| worker 拒绝任务（满载） | 返回 503，WorkerRegistry 暂时跳过该 worker，选下一个 |
| 任务执行异常 | worker 标 failed + error 回传，大脑按派发规则重试 1 次 |
| 心跳超时 | 标 lost，可重派 |
| 文件拉取失败 | 安全关卡②报错，大脑决定重取或回退本地 |

### 关键设计点

- 任务异步 + 轮询，HTTP 不阻塞，长任务可承载
- 文件按需拉取，关卡②检验代码时才触发
- 任务持久化让 worker 崩溃可恢复、可重派
- 故障处理收敛在 WorkerRegistry + TaskStore，大脑流程不感知故障细节

---

## 6 文件结构与扩展点

### 6.1 目录结构（V2 新增部分）

```
d:\agent\agent\
├── workspace/agent/orchestrator/
│   ├── SKILL.md                       # 不变
│   ├── security-rules/                # 不变
│   ├── memory/context/                # 不变
│   └── runtime/                       # V2 新增：大脑侧运行时
│       ├── dispatcher.py              # Dispatcher 接口 + Local/Remote 实现
│       ├── worker_registry.py         # 注册+心跳+选择
│       ├── task_store.py              # 任务持久化
│       ├── memory_store.py            # Memory 接口 + File 实现
│       ├── brain_api.py               # 大脑 HTTP 服务（接收注册/心跳）
│       └── config.py                  # 本地/远程模式开关、brain_url
│
├── worker/                            # V2 新增：worker 侧（可独立部署到其他主机）
│   ├── worker_api.py                  # FastAPI 服务
│   ├── agent_runner.py                # Agent SDK 执行体
│   ├── worker_config.py               # brain_url, worker_id, role, work_dir
│   └── requirements.txt               # fastapi, claude-agent-sdk, uvicorn
│
└── .claude/agents/executor.md         # 不变（worker 复用此定义）
```

### 6.2 各单元接口

| 单元 | 责任 | 接口 | 依赖 |
|------|------|------|------|
| `dispatcher.py` | 派发抽象 | `dispatch(role, prompt, schema, model) -> DispatchResult` | Local 用 Agent 工具；Remote 用 worker_registry + task_store |
| `worker_registry.py` | worker 发现 | `register(w) / heartbeat(id) / select_worker(role) -> worker` | brain_api 写入 |
| `task_store.py` | 任务持久化 | `write / update / get / list_lost / list_by_worker` | 本地 JSON 文件 |
| `memory_store.py` | 记忆访问抽象 | `read_core / write_core / read_context / write_context` | 本地文件（V2） |
| `brain_api.py` | 大脑 HTTP 端点 | `/register /heartbeat /tasks` | worker_registry, task_store |
| `worker/worker_api.py` | worker HTTP 端点 | `/task /task/{id} /artifact/{id}` | agent_runner |
| `worker/agent_runner.py` | SDK 执行 | `run(task) -> result` | claude-agent-sdk, executor.md |

### 6.3 V1→V2 切换点

- **配置驱动**：`config.py` 里 `mode = "local" | "remote"`
  - `local`：Dispatcher 用 LocalDispatcher，不启动 brain_api，V1 单机行为完全保留
  - `remote`：Dispatcher 用 RemoteDispatcher，启动 brain_api 接收 worker 注册
- **SKILL.md 不改**：大脑流程描述还是「派发 executor」，不感知 local/remote
- **executor.md 不改**：worker 直接复用现有角色定义

### 6.4 扩展点

1. **加新 worker 角色**：未来远程化 security-guard 时，新增 `security-worker/` + WorkerRegistry 支持多角色选择。本次不做。
2. **worker 选择策略升级**：`select_worker` V2 轮询，预留按负载/能力匹配（心跳已带 load）。
3. **记忆层换共享存储**：`MemoryStore` 接口已抽象，换 Redis/DB 实现即可，共识流程不变。
4. **任务持久化换 DB**：`TaskStore` V2 用 JSON 文件，换 SQLite/DB 接口不变。
5. **多大脑/高可用**：当前 1 大脑，WorkerRegistry 与 TaskStore 接口化后可扩展为多大脑共享。

### 6.5 有意不做（YAGNI）

- 不做 worker 间直连：所有协作经大脑
- 不做实时流式进度：V2 用轮询
- 不做任务取消强保证：`/cancel` 标记意图，不保证立即终止 SDK 会话
- 不做 worker 自动扩缩容：手动启动注册
- 不做 TLS/鉴权：V2 假设内网可信，预留 `config` 加 auth 字段（生产前必须补）

---

## 7 测试策略

- **Dispatcher 单测**：LocalDispatcher 走 V1 路径；RemoteDispatcher mock HTTP 验证派发/轮询/结果回收。
- **WorkerRegistry 单测**：注册、心跳超时剔除、select_worker 轮询。
- **TaskStore 单测**：状态流转 pending→running→done/failed/lost、list_lost、list_by_worker。
- **worker 服务集成测**：起 worker_api + agent_runner（mock SDK），验证 /task 接受、/task/{id} 查询、/artifact 拉取。
- **故障恢复测**：模拟心跳超时 → lost → 重派；worker 恢复 → 接续。
- **V1 回归**：`mode=local` 下 V1 单机行为不变，V1 测试全通过。

---

*设计日期：2026-07-01*
