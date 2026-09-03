# 跨主机部署 + SDK 执行 完整指导手册

> 目标：把 executor 部署到远程主机，用真实 `claude-agent-sdk` 执行任务（而非 fake 测试桩），实现大脑主机与一台或多台 worker 主机的跨机协同。
>
> 适用场景：多机并行执行、利用远程机器算力/环境、生产化部署。
>
> 推荐节奏：**先用 fake 模式打通网络链路，再切 sdk 模式接入真实执行**。前者不消耗 API、不需要认证，能快速定位网络/配置问题；后者在此基础上叠加认证与 executor 定义。

---

## 0. 与 fake 模式的区别（先读）

| 维度 | fake 模式 | **sdk 模式（本手册）** |
|------|-----------|----------------------|
| 执行 | 直接返回假结果字符串 | 真实起 claude-agent-sdk 会话执行 |
| 依赖 | fastapi/uvicorn/requests | 额外需要 `claude-agent-sdk` |
| 认证 | 不需要 | **必须**（`ANTHROPIC_AUTH_TOKEN` 等 SDK 环境变量） |
| executor.md | 不需要 | **必须就位**（定义 executor 角色） |
| 成本 | 零 | 消耗 Anthropic API 调用 |
| 产物 | 无 | 真实文件（代码/报告等），可拉取与安全检验 |

切到 sdk 前，确保已理解认证与 executor.md 两项前置。

---

## 1. 架构与拓扑

### 1.1 部署拓扑

```
大脑主机（Brain）                        worker 主机（Worker，可多台）
┌─────────────────────────┐            ┌─────────────────────────┐
│ 主会话 (orchestrator)    │            │ worker_api (FastAPI)     │
│   └─ Bash 调 dispatch_cli │            │   POST /task            │
│         │ HTTP 转发        │  ①派发     │   GET  /task/{id}       │
│         ▼                 │ ─────────▶│   GET  /artifact/{id}   │
│ brain_api (0.0.0.0:7000) │            │   └─ SdkAgentRunner     │
│   /dispatch /register     │  ②注册+心跳│       ├ claude-agent-sdk │
│   /heartbeat /workers     │ ◀─────────│       ├ executor.md      │
│   /proxy/artifact /stats  │            │       └ 认证(key/登录态)  │
│   └─ 同源托管 web/dist     │  ③拉产物   │                          │
│                           │ ◀─────────│                          │
│ security-guard (本地)     │            │ work_dir/{task_id}/ 产物 │
└─────────────────────────┘            └─────────────────────────┘
```

三条数据通路：① 派发（brain -> worker `/task` + 轮询 `/task/{id}`）；② 注册与心跳（worker -> brain）；③ 产物回传（brain 经 `/proxy/artifact` 代理向 worker `/artifact/{id}` 拉取）。

### 1.2 两台主机角色

| 主机 | 运行内容 | 端口 | 是否可多台 |
|------|---------|------|-----------|
| 大脑主机 | brain_api + 前端（可选）+ security-guard（本地子 agent） | 7000 | 单台 |
| worker 主机 | worker_api + SdkAgentRunner | 8001（可自定义） | 可多台 |

**security-guard 始终在大脑本地**，不跨主机。

---

## 2. 前置条件

### 2.1 网络
- 两台主机在同一局域网或路由可达，能互相 `ping` 通。
- 获取双方局域网 IP（如大脑 `172.16.2.95`，worker `172.16.2.x`）。
- 确认不是 `127.0.0.1` / `localhost` —— 跨主机必须用真实 IP。

### 2.2 环境
- 两台主机均装 Python 3.11+（实测支持 3.13）。
- 大脑主机若要 LLM 增强安全检验，还需装 `claude-agent-sdk`（见 3.3）。

### 2.3 SDK 认证（关键前置，最易卡住）

`claude-agent-sdk` 需要有效认证才能调用。**本机实测确认：本机通过火山引擎 Ark 网关接入，认证依赖以下环境变量，而非 `ANTHROPIC_API_KEY`：**

| 变量 | 本机实测值 | 作用 |
|------|-----------|------|
| `ANTHROPIC_AUTH_TOKEN` | 已设（Bearer token，约 46 位） | 认证凭证 |
| `ANTHROPIC_BASE_URL` | `https://ark.cn-beijing.volces.com/api/plan` | 网关地址（火山引擎 Ark） |
| `ANTHROPIC_MODEL` | `ark-code-latest` | 默认模型名 |

`claude-agent-sdk`（与 Claude Code 同源）读取这套环境变量：把请求发往 `ANTHROPIC_BASE_URL` 指定的网关，以 `ANTHROPIC_AUTH_TOKEN` 作 Bearer 认证。**远程 worker 主机必须复制同一套环境变量**才能执行 sdk 任务。

> 若你的环境是 Anthropic 官方直连（非网关），则改用 `ANTHROPIC_API_KEY=sk-ant-...`。两者二选一，不要混设。

**验证认证是否可用**（在 worker 主机执行，成功打印 `OK` 即认证就绪）：
```bash
python -c "
import asyncio, claude_agent_sdk as sdk
async def t():
    o = sdk.ClaudeAgentOptions(model='haiku', max_turns=1, permission_mode='default')
    async for m in sdk.query(prompt='reply with exactly: OK', options=o):
        if isinstance(m, sdk.ResultMessage): print('认证可用:', repr(m.result))
asyncio.run(t())
"
```

> 若该脚本报认证错误（401/未授权），检查 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` 是否已设且有效，或改用 `ANTHROPIC_API_KEY`。先解决认证再继续。
>
> 模型名：executor.md 默认 `model=sonnet`。若网关不识别该名称，派发时用 `ORCH_MODEL` 覆盖为网关支持的模型（如 `ark-code-latest`）。

---

## 3. 大脑主机部署

### 3.1 代码就位与依赖
```bash
cd <项目根>/workspace/agent/orchestrator/runtime
pip install fastapi uvicorn requests httpx pyyaml
```

### 3.2 关键配置
大脑需对外监听（默认硬编码 `127.0.0.1` 仅本机可达）。[brain_api.py](../workspace/agent/orchestrator/runtime/brain_api.py) 已支持 `ORCH_BRAIN_HOST` 环境变量：

| 变量 | 值 | 说明 |
|------|----|------|
| `ORCH_MODE` | `remote` | 启用跨主机派发 |
| `ORCH_BRAIN_HOST` | `0.0.0.0` | 对所有网卡监听，允许远程 worker 连入 |
| `ORCH_BRAIN_URL` | `http://<大脑IP>:7000` | 供 dispatch_cli 转发（默认 127.0.0.1，跨主机需改成大脑真实 IP） |

### 3.3 [可选] security-guard LLM 增强
默认安全检验用确定性规则引擎。开启 LLM 增强（规则 pass 后调 haiku 补判"规则之外"可疑情况）需大脑侧装 `claude-agent-sdk`：
```bash
pip install claude-agent-sdk
# 启动时设
export ORCH_SECURITY_LLM=1
```
异常时自动降级回规则引擎，不影响主流程。

### 3.4 启动大脑
```bash
cd <项目根>/workspace/agent/orchestrator/runtime
export ORCH_MODE=remote
export ORCH_BRAIN_HOST=0.0.0.0
export ORCH_BRAIN_URL=http://<大脑IP>:7000
python -m brain_api
```
看到 `Uvicorn running on http://0.0.0.0:7000` 即成功。

### 3.5 防火墙放行 7000（管理员）
- **Windows**：`netsh advfirewall firewall add rule name="orch-brain" dir=in action=allow protocol=TCP localport=7000`
- **Linux**：`sudo ufw allow 7000/tcp`（或对应 firewalld 命令）

### 3.6 验证大脑对外可达
在 **worker 主机** 上访问大脑（用大脑 IP，不是 localhost）：
```bash
curl http://<大脑IP>:7000/stats
```
返回 JSON（含 `"mode":"remote"`）即通。若超时/拒绝，检查大脑是否监听 `0.0.0.0`、防火墙、双方是否同网段。

---

## 4. worker 主机部署（SDK 模式重点）

### 4.1 拷贝文件

SDK 模式必需两块文件，且**必须保持相对目录结构**（因为 `executor.md` 的默认路径基于 `worker/` 的 `__file__` 向上一级定位 `.claude/agents/executor.md`）：

```
<部署根>/                     # 任意目录，但下面两个子目录必须同级
├── worker/                   # 整个 worker 目录
│   ├── worker_api.py
│   ├── agent_runner.py
│   ├── worker_config.py
│   └── requirements.txt
└── .claude/
    └── agents/
        └── executor.md       # executor 角色定义（model=sonnet，全工具）
```

> 若无法保持该结构，改用 `ORCH_EXECUTOR_DEF` 指定 executor.md 绝对路径（见 4.6）。

拷贝方式（示例，从大脑主机传到 worker 主机）：
```bash
# 在大脑主机执行，打包必需文件
tar czf orch-worker.tar.gz worker/ .claude/agents/executor.md
scp orch-worker.tar.gz <user>@<workerIP>:~/

# 在 worker 主机解压
tar xzf orch-worker.tar.gz
```

### 4.2 安装依赖
```bash
cd <部署根>/worker
pip install -r requirements.txt
```
`requirements.txt` 含 `fastapi`、`uvicorn`、`requests`、`claude-agent-sdk`。确认 sdk 装好：
```bash
python -c "import claude_agent_sdk; print(claude_agent_sdk.__name__, 'OK')"
```

### 4.3 配置 SDK 认证
按 2.3，把与大脑主机相同的一套 SDK 环境变量复制到 worker 主机（火山引擎 Ark 网关接入）：
```bash
export ANTHROPIC_AUTH_TOKEN=<你的Bearer token>
export ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/plan
export ANTHROPIC_MODEL=ark-code-latest
# （官方直连场景则改用：export ANTHROPIC_API_KEY=sk-ant-...）
```
并运行 2.3 的验证脚本确认认证可用。**这一步不通过，后面所有 sdk 任务都会失败。**

> 建议把这三个变量写入 worker 主机的 `~/.bashrc`（或 Windows 系统环境变量），避免每次启动 worker 都要重设。

### 4.4 确认 executor.md 就位
默认路径自动定位到 `<部署根>/.claude/agents/executor.md`。若目录结构不是 4.1 所示，显式指定：
```bash
export ORCH_EXECUTOR_DEF=/abs/path/to/executor.md
```

### 4.5 选择权限模式
`ORCH_PERMISSION_MODE` 控制 sdk 执行时遇到需确认操作（写文件、执行命令等）的行为：

| 值 | 行为 | 适用场景 |
|----|------|---------|
| `bypassPermissions`（默认） | 跳过所有确认，无人值守 | **跨主机推荐**，worker 无人值守 |
| `acceptEdits` | 自动接受文件编辑，其他仍确认 | 受限 |
| `default` | 全部需确认（会卡住） | 仅交互调试，**勿用于跨主机** |

> 安全提示：`bypassPermissions` 下 executor 拥有全工具且不停顿，这正是 security-guard 强制安全关卡存在的意义——指令与产物都会被检验，危险指令在派发前即被 block。

### 4.6 完整环境变量配置

```bash
cd <部署根>/worker
export ORCH_BRAIN_URL=http://<大脑IP>:7000     # 指向大脑
export ORCH_WORKER_ID=worker-B                 # 唯一标识（勿与 worker-A 重名）
export ORCH_WORKER_HOST=<worker本机真实IP>      # 关键！大脑靠它回连拉产物
export ORCH_WORKER_PORT=8001                   # 监听端口
export ORCH_RUNNER=sdk                         # ★ 切到真实 SDK 执行
export ORCH_PERMISSION_MODE=bypassPermissions  # 无人值守权限
# export ORCH_EXECUTOR_DEF=/abs/path/executor.md  # 仅目录结构不符时设
# export ANTHROPIC_AUTH_TOKEN=...                # 见 4.3，SDK 认证三件套（网关接入）
# export ANTHROPIC_BASE_URL=...
# export ANTHROPIC_MODEL=...
export ORCH_AUTO_REGISTER=1                    # 启动自动注册到大脑（默认开）
```

> **`ORCH_WORKER_HOST` 绝不能填 `127.0.0.1`**：该值同时是 worker 的监听绑定地址和注册上报地址，大脑会按此 IP 反向连接 worker 拉产物。填 127.0.0.1 会导致大脑去连它自己，产物回传必然失败。

### 4.7 启动 worker
```bash
python -m worker_api
```
看到 `Uvicorn running on http://<workerIP>:8001` 即成功。启动时后台线程会自动向大脑注册并发心跳。

### 4.8 防火墙放行 8001（管理员）
- **Windows**：`netsh advfirewall firewall add rule name="orch-worker" dir=in action=allow protocol=TCP localport=8001`
- **Linux**：`sudo ufw allow 8001/tcp`

### 4.9 验证 worker 注册成功
在 **大脑主机** 查看注册节点：
```bash
curl http://127.0.0.1:7000/workers
```
应看到 `worker_id=worker-B`、`host=<workerIP>`、`port=8001` 的记录，且 `last_heartbeat` 时间戳持续更新（说明心跳活跃）。

---

## 5. 端到端验证

### 5.1 派发真实 SDK 任务
```bash
cd <项目根>
export ORCH_BRAIN_URL=http://<大脑IP>:7000
python workspace/agent/orchestrator/runtime/dispatch_cli.py "在当前目录写一个 hello.py，内容为打印 hello world"
```
预期返回：
```json
{"task_id": "task-xxxxxxxx", "status": "done", "output": "...", "artifacts": ["hello.py"], "error": null, "product_verdict": {"verdict": "pass", ...}}
```
- `status=done` + 非空 `artifacts` 说明 sdk 真实执行并产出了文件。
- `product_verdict` 是安全关卡②对产物的检验结果。

### 5.2 覆盖执行模型
executor.md 默认 `model=sonnet`。复杂任务可临时升档，简单任务降档省成本：
```bash
ORCH_MODEL=sonnet python .../dispatch_cli.py "..."   # 或 opus / haiku
```
`ORCH_MODEL` 经 dispatch_cli -> /dispatch -> worker /task 传到 SdkAgentRunner，覆盖 executor.md 的默认 model。

### 5.3 拉取产物文件
产物留在 worker 的 `work_dir/{task_id}/` 下。经大脑代理拉取（前端统一连大脑）：
```bash
curl "http://127.0.0.1:7000/proxy/artifact?worker_id=worker-B&task_id=task-xxxxxxxx&path=hello.py"
```
返回 `{"content": "print('hello world')..."}`。文本类产物（.py/.md/.json 等）会自动参与安全检验。

### 5.4 前端查看
打开 `http://<大脑IP>:7000/`（生产同源）或 `http://localhost:5173/`（dev）：
- **状态总览**：看到 worker-B 节点卡片，展开可见其任务与产物在线预览。
- **命令台**：图形化派发 prompt（可填 role/model/上下文记忆）。
- **节点管理**：查看心跳、注销节点、任务取消/重派。

### 5.5 多 worker 扩展
重复第 4 章再部署一台 `worker-C`（`ORCH_WORKER_ID=worker-C`、不同端口/IP）。大脑默认 `round_robin` 轮询分配任务；改 `ORCH_SELECT_STRATEGY=least_load` 按负载选。

---

## 6. 配置参考（完整环境变量表）

### 大脑侧
| 变量 | 默认 | 说明 |
|------|------|------|
| `ORCH_MODE` | local | `remote` 跨主机 |
| `ORCH_BRAIN_HOST` | 127.0.0.1 | 监听地址，跨主机设 `0.0.0.0` |
| `ORCH_BRAIN_URL` | http://127.0.0.1:7000 | 大脑地址（dispatch_cli 转发目标） |
| `ORCH_HEARTBEAT_TIMEOUT` | 45 | 心跳超时阈值（秒），超时剔除 worker |
| `ORCH_SELECT_STRATEGY` | round_robin | worker 选择策略：`round_robin` / `least_load` |
| `ORCH_SECURITY_LLM` | 0 | 1 开启 security-guard LLM 增强（需大脑侧 sdk） |
| `ORCH_CORS_ORIGINS` | http://localhost:5173 | 允许的前端来源（逗号分隔） |
| `ORCH_WEB_DIST` | 自动查找 web/dist | 前端构建产物目录（同源托管） |

### worker 侧
| 变量 | 默认 | 说明 |
|------|------|------|
| `ORCH_BRAIN_URL` | http://127.0.0.1:7000 | 大脑地址（跨主机改成大脑 IP） |
| `ORCH_WORKER_ID` | worker-local | worker 唯一标识 |
| `ORCH_WORKER_HOST` | 127.0.0.1 | 监听+上报地址（跨主机必填真实 IP） |
| `ORCH_WORKER_PORT` | 8000 | 监听端口 |
| `ORCH_RUNNER` | fake | **sdk 模式必设 `sdk`** |
| `ORCH_EXECUTOR_DEF` | 自动定位 | executor.md 路径（目录结构不符时显式指定） |
| `ORCH_WORK_DIR` | worker/.worker_work | worker 本地工作区（产物落盘处） |
| `ORCH_PERMISSION_MODE` | bypassPermissions | sdk 权限模式 |
| `ORCH_AUTO_REGISTER` | 1 | 启动自动注册到大脑 |
| `ANTHROPIC_AUTH_TOKEN` | - | SDK 认证 Bearer token（网关接入必设） |
| `ANTHROPIC_BASE_URL` | - | 网关地址（如火山引擎 Ark，配合 AUTH_TOKEN） |
| `ANTHROPIC_MODEL` | - | 默认模型名（网关接入时） |
| `ANTHROPIC_API_KEY` | - | SDK 认证（官方直连场景用，与 AUTH_TOKEN 二选一） |

### 派发侧
| 变量 | 默认 | 说明 |
|------|------|------|
| `ORCH_MODEL` | executor.md 的 sonnet | 覆盖执行模型 |
| `ORCH_CONTEXT_MEMORY` | - | 注入任务的上下文记忆 |

---

## 7. 故障排查

| 现象 | 排查 |
|------|------|
| worker 启动后大脑 `/workers` 看不到它 | ① `ORCH_BRAIN_URL` 是否指向大脑真实 IP（非 127.0.0.1）；② 大脑是否 `ORCH_BRAIN_HOST=0.0.0.0` 且防火墙放行 7000；③ worker 日志有无注册失败重试 |
| worker 上线后很快消失 | 心跳超时。确认 worker 进程活着、`ORCH_WORKER_HOST` 正确、大脑能连到 worker 8001（防火墙） |
| 任务 `failed`，error 含 `sdk error: ...` | 多为认证问题。运行 2.3 验证脚本；确认 `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` 已设有效（或官方直连的 `ANTHROPIC_API_KEY`） |
| 任务 `failed`，error 含 `executor.md` 或路径 | `ORCH_EXECUTOR_DEF` 路径错或文件不存在。检查 4.1 目录结构 / 4.4 路径配置 |
| 任务一直 `running` 不返回 | sdk 执行中或卡住。`GET /task/{id}` 轮询；复杂任务耗时长，可调大耐心；卡死可标 lost 后重派 |
| 产物拉取 404 / 502 | `ORCH_WORKER_HOST` 填了 127.0.0.1（大脑连不回），或 worker 8001 防火墙未放行，或 path 越界（防路径穿越校验拦截） |
| 派发返回 `无可用 worker` | worker 未注册或已超时。`GET /workers` 确认；启动 worker 并等其注册 |
| `ModuleNotFoundError: claude_agent_sdk` | worker 依赖未装全。`pip install -r requirements.txt` |
| `permission_mode=default` 卡住 | 跨主机勿用 default。改 `ORCH_PERMISSION_MODE=bypassPermissions` 重启 worker |
| 大脑重启后 worker 不见了 | worker 注册只在启动时发一次，大脑重启会丢记录。**重启大脑后 worker 也要重启**才能重新注册 |

### SDK 认证专项排查
- 运行 2.3 验证脚本，看是否打印 `认证可用`。
- 若报 401/认证错误：确认 `ANTHROPIC_AUTH_TOKEN` 与 `ANTHROPIC_BASE_URL` 均已设且 token 有效；若是官方直连场景，确认 `ANTHROPIC_API_KEY` 有效。两者不要混设。
- worker 进程能否读到这些环境变量：在启动 worker 的同一 shell 里 `echo $ANTHROPIC_AUTH_TOKEN` / `echo $ANTHROPIC_BASE_URL` 确认（非空）。
- 网关不识别模型名时，派发用 `ORCH_MODEL=ark-code-latest` 覆盖 executor.md 默认的 sonnet。

---

## 8. 生产化与安全建议

1. **TLS 与鉴权**：V2 默认假设内网可信，HTTP 明文且无鉴权。上生产需补 TLS（反向代理终结）与 token 鉴权，防止 worker 被冒充注册、指令被窃听。
2. **权限模式取舍**：`bypassPermissions` 无人值守但 executor 全工具不停顿，务必依赖 security-guard 双关卡（指令+产物）兜底；高敏感场景考虑 `acceptEdits` 并接受需人工介入的代价。
3. **API 成本控制**：sdk 模式按 token 计费。简单任务用 `ORCH_MODEL=haiku` 降档；复杂任务才升 sonnet/opus。关注 Anthropic 用量面板。
4. **工作目录隔离**：每个 task 在 `work_dir/{task_id}/` 独立目录执行，互不污染；定期清理历史产物。
5. **多 worker 高可用**：部署多台 worker，`least_load` 策略均衡负载；单 worker 故障不影响整体（大脑剔除失联节点后轮询到其他）。
6. **产物安全检验**：保持 `default.yaml` 规则与业务匹配，用 `overrides/user.yaml` 定制；高敏感场景开启 `ORCH_SECURITY_LLM=1`。
7. **持久化**：TaskStore 当前为 JSON 文件，大脑重启不丢任务但丢 worker 注册；生产可换 DB；WorkerRegistry 可考虑持久化或依赖 worker 重启重注册。
8. **凭证保护**：`ANTHROPIC_AUTH_TOKEN` 是敏感凭证，勿提交代码库、勿打印日志；worker 主机用环境变量或密钥管理服务注入，避免硬编码到启动脚本。

---

## 9. 快速命令清单（Cheat Sheet）

```bash
# === 大脑主机 ===
cd <项目根>/workspace/agent/orchestrator/runtime
export ORCH_MODE=remote ORCH_BRAIN_HOST=0.0.0.0 ORCH_BRAIN_URL=http://<大脑IP>:7000
python -m brain_api
# 防火墙(管理员): netsh advfirewall firewall add rule name="orch-brain" dir=in action=allow protocol=TCP localport=7000

# === worker 主机（SDK 模式）===
cd <部署根>/worker
pip install -r requirements.txt
# 认证(网关): export ANTHROPIC_AUTH_TOKEN=... ANTHROPIC_BASE_URL=https://ark.cn-beijing.volces.com/api/plan ANTHROPIC_MODEL=ark-code-latest
#   (官方直连改用: export ANTHROPIC_API_KEY=sk-ant-...)
export ORCH_BRAIN_URL=http://<大脑IP>:7000
export ORCH_WORKER_ID=worker-B
export ORCH_WORKER_HOST=<worker真实IP>
export ORCH_WORKER_PORT=8001
export ORCH_RUNNER=sdk
export ORCH_PERMISSION_MODE=bypassPermissions
python -m worker_api
# 防火墙(管理员): netsh advfirewall firewall add rule name="orch-worker" dir=in action=allow protocol=TCP localport=8001

# === 验证 ===
curl http://127.0.0.1:7000/workers                              # 大脑查注册节点
python .../dispatch_cli.py "写一个 hello.py 打印 hello world"  # 派发 sdk 任务
curl "http://127.0.0.1:7000/proxy/artifact?worker_id=worker-B&task_id=<id>&path=hello.py"  # 拉产物
```

---

*手册版本：2026-07-12 · 基于 brain_api（含 ORCH_BRAIN_HOST 改造）、worker_api、SdkAgentRunner 实测编写*
