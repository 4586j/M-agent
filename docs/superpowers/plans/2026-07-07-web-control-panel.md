# 多 Agent 编排框架 — Web 控制面板 实施方案

> 日期：2026-07-07
> 目标：为现有 brain_api / worker_api 多 agent 编排框架补一套 Web 控制面板，覆盖三大功能：① 状态检测（节点 + 任务 + 进度）② 节点管理 ③ 对大脑发布命令/询问。

---

## 0. 现状摘要（已勘探）

- 后端纯 Python（FastAPI），无任何前端代码。
- `brain_api`（端口 7000）：`POST /register`、`POST /heartbeat`、`POST /dispatch`、`GET /workers`、`GET /tasks?worker_id=`。
- `worker_api`（端口 8000+）：`POST /task`、`GET /task/{id}`、`GET /artifact/{id}?path=`。
- `brain_api` 无 CORS；`worker_api` 无自动心跳、无自动注册（手册列为待办）。
- `WorkerRegistry` 无 `unregister`；`select_worker` 仅轮询，未用 load。
- `TaskStore` 状态集：`pending|running|done|failed|lost`，无 cancelled、无单条 GET。
- `Dispatcher` 无 redispatch。

用户决策：**现代构建工程（Vite + React + TS）+ 后端全面扩展**。

---

## 1. 技术选型

| 项 | 选择 | 理由 |
|----|------|------|
| 前端框架 | React 18 + TypeScript | 类型安全对接 API schema，生态成熟 |
| 构建 | Vite 5 | 快，零配置 TS/React |
| 样式 | Tailwind CSS v3 | 快速产出一致 UI，无需手写大量 CSS |
| 路由 | React Router v6 | 三视图切换 |
| HTTP | 原生 fetch 封装 | 轻量，无额外依赖 |
| 实时性 | setInterval 轮询（5s） | 简单可靠，后端无 WS |

前端目录：项目根新建 `web/`。

---

## 2. 后端扩展（全面扩展）

### 2.1 `workspace/agent/orchestrator/runtime/worker_registry.py`
- 新增 `unregister(worker_id)`：从 `_workers` 与 `_rr` 移除。
- `select_worker` 增加按负载排序选项：`strategy="round_robin"|"least_load"`（默认 round_robin 保持兼容；心跳 load 字段已存在但未用）。

### 2.2 `workspace/agent/orchestrator/runtime/task_store.py`
- `VALID_STATUSES` 追加 `"cancelled"`。
- 新增 `get(task_id)` 已有；确保 `update` 支持 cancelled。

### 2.3 `workspace/agent/orchestrator/runtime/dispatcher.py`
- `Dispatcher` 新增 `redispatch(task_id)`：读原 TaskRecord，用同 role/prompt/context_memory/model 重新 `dispatch`，返回新 DispatchResult。
- `RemoteDispatcher` 实现 redispatch：选 worker、写新 pending、POST /task、轮询。

### 2.4 `workspace/agent/orchestrator/runtime/brain_api.py`
- **CORS**：`CORSMiddleware`，开发期允许 `http://localhost:5173`，生产同源（可配 `ORCH_CORS_ORIGINS`）。
- **静态托管**：`app.mount("/", StaticFiles(directory=web_dist, html=True))`，`web_dist` 由 `ORCH_WEB_DIST` 指向 `web/dist`；目录不存在时跳过（不阻断 API）。
- **新端点**：
  - `DELETE /workers/{worker_id}` → 注销。
  - `GET /tasks/{task_id}` → 单任务详情（404 if missing）。
  - `POST /tasks/{task_id}/cancel` → 标记 cancelled（仅 pending/running 可取消；记录原状态）。
  - `POST /tasks/{task_id}/redispatch` → 重派，返回新 task。
  - `GET /stats` → 聚合：worker 总数/活跃数、任务按状态计数、最近任务。
  - `GET /proxy/artifact?worker_id=&task_id=&path=` → 代理拉取 worker 产物（前端统一连 brain，避免直连 worker 跨域/记地址）。
- `_build_app` 装配 dispatcher 时注入 redispatch 所需依赖。

### 2.5 `worker/worker_api.py`
- **CORS**：同上。
- **自动注册**：启动时后台线程 `POST {brain_url}/register`（带 worker_id/role/host/port/capabilities），失败重试 3 次。
- **自动心跳**：后台线程按 `ORCH_HEARTBEAT_INTERVAL`（默认 15s）`POST {brain_url}/heartbeat`，修复手册待办。
- 保留手动注册/心跳端点兼容。

### 2.6 配置与环境变量（新增）
- `ORCH_CORS_ORIGINS`（默认 `http://localhost:5173`）。
- `ORCH_WEB_DIST`（默认 `web/dist`）。
- `ORCH_SELECT_STRATEGY`（默认 `round_robin`）。
- worker 侧复用 `ORCH_BRAIN_URL`、`ORCH_HEARTBEAT_INTERVAL`。

### 2.7 测试
- `runtime/tests/` 补：`test_registry_unregister`、`test_brain_cancel_redispatch`、`test_brain_stats`、`test_brain_proxy_artifact`、`test_dispatcher_redispatch`。
- `worker/tests/` 补：自动注册/心跳用 `now_fn`/mock http 可测的部分（避免真起线程，抽 `register_to_brain()` 函数单测）。
- 目标：现有 30 passed 不回归，新增用例全绿。

---

## 3. 前端工程 `web/`

### 3.1 工程骨架
```
web/
├── package.json          # react, react-dom, react-router-dom, vite, typescript, tailwind
├── vite.config.ts        # dev proxy: /api → http://127.0.0.1:7000
├── tsconfig.json
├── tailwind.config.js
├── postcss.config.js
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx           # 布局 + 路由 + 顶栏（大脑状态徽标 + 自动刷新开关）
    ├── api/client.ts     # fetch 封装，统一前缀 /api，类型化
    ├── types.ts          # WorkerInfo / TaskRecord / DispatchResult / Stats
    ├── hooks/usePolling.ts
    ├── views/
    │   ├── Dashboard.tsx     # 功能①状态检测
    │   ├── NodeManage.tsx    # 功能②管理
    │   └── Command.tsx       # 功能③命令台
    └── components/
        ├── WorkerCard.tsx    # 节点卡片，点击展开任务
        ├── TaskList.tsx
        ├── TaskDetail.tsx    # output/artifacts/error + 产物预览
        ├── StatBar.tsx
        └── StatusBadge.tsx
```

### 3.2 视图设计

**① Dashboard（状态检测）**
- 顶栏 StatBar：大脑 mode、worker 总数/活跃、任务各状态计数（pending/running/done/failed/cancelled/lost）。
- 节点网格：每张 `WorkerCard` 显示 worker_id、role、`host:port`、活跃状态（由 last_heartbeat 与 timeout 比对，或 /workers 已过滤）、load、运行中任务数。
- **点击节点** → 展开该节点任务列表（`GET /tasks?worker_id=`），任务项显示 task_id、status 徽标、prompt 摘要、updated_at、进度（running 显示运行中）。
- **点击任务** → `TaskDetail` 抽屉：完整 output、artifacts 列表（点击经 `/proxy/artifact` 预览内容）、error。
- 5s 轮询刷新（可暂停）。

**② NodeManage（管理）**
- 注册新 worker 表单（worker_id/role/host/port/capabilities JSON）→ `POST /register`。
- 节点操作：注销（`DELETE /workers/{id}`，二次确认）、刷新心跳（`POST /heartbeat`）。
- 任务操作：取消（`POST /tasks/{id}/cancel`）、重派（`POST /tasks/{id}/redispatch`）。
- 操作后即时刷新列表 + toast 反馈。

**③ Command（命令台）**
- 表单：prompt（多行）、role（默认 executor）、model（可选）、context_memory（可选）。
- 发送 → `POST /dispatch`，展示 task_id/status/output/artifacts/error。
- 历史命令列表（本地状态，可重发）。
- 长任务：dispatch 同步返回结果（后端轮询完成才返回）；前端 loading 态 + 超时提示。

### 3.3 开发与部署
- **开发**：`cd web && npm install && npm run dev`（5173），Vite proxy `/api` → brain 7000，无 CORS 烦恼。
- **生产**：`npm run build` 产 `web/dist`；`ORCH_WEB_DIST=web/dist python -m brain_api` 同源托管，访问 `http://127.0.0.1:7000/`。
- API 前缀：前端统一用 `/api/...`，dev 走 proxy，prod 需 brain 路由把 `/api/*` 映射到现有端点（或 proxy 配置 + 生产用相对路径同源直调）。**简化**：生产同源时前端直接调 `/workers` 等（无 /api 前缀），dev proxy 把 `/workers` 等转发到 7000。最终 client 用相对路径，dev/prod 一致。

---

## 4. 实施顺序

1. 后端扩展（registry/task_store/dispatcher/brain_api/worker_api）+ 测试，跑绿。
2. `web/` 工程脚手架 + Tailwind + 路由 + API client + types。
3. Dashboard 视图 + WorkerCard/TaskDetail。
4. NodeManage 视图。
5. Command 视图。
6. 联调：启动 brain(remote) + worker(fake)，端到端验证三功能。
7. 更新 `docs/使用手册.md` 增「Web 控制面板」章节。

---

## 5. 不做 / 后续
- 不引入状态管理库（Redux/Zustand）—— React state + hooks 足够。
- 不做 WebSocket 实时推送 —— 轮询够用。
- 不做登录鉴权 —— 沿用内网可信假设（手册生产化清单已列 TLS/鉴权）。
- security-guard 仍在主会话本地，面板不直接编排它（保留给大脑 skill）。

---

## 6. 风险
- worker 自动心跳改后端行为，需确保不破坏现有「手动心跳」测试（保留端点）。
- Tailwind v4 与 v3 配置差异 —— 锁 v3 稳定。
- Windows 下 npm/node 需用户已装；若未装会卡在 `npm install`，届时提示。
