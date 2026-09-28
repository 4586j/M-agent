# 统一技能 / MCP 仓库（skillhub）

本目录是技能的**对账层**：技能源不搬家（仍在 `../../skills/<name>/`），这里用
`catalog.json`（单一事实源）记录每个技能的**逐文件 sha256** 与**评审标记**，
让新节点能用一条命令安装「检验过」的技能、统一生成 MCP 配置。

「检验过」是双层门：

| 门 | 内容 | 强度 |
|---|---|---|
| 完整性（硬门） | 源文件 sha256 与 catalog 逐文件比对，不符即拒 | 不可绕过（`--include-experimental` 也不豁免） |
| 评审（软门） | `status: verified` 标记，install 默认只装它 | `--include-experimental` 可放行 experimental |

所有命令从**仓库根**执行：`python -m agent_core.skillhub <子命令>`

---

## 用前须知：改了技能源，先 `update`

`catalog.json` 的 hash 是安装时对账基准。**任何对 `workspace/agent/skills/<name>/` 的改动**
（新增/删改文件）都会让源与 catalog 漂移，install 会**拒绝**该技能（防装到半成品）。

改完后必须重算 hash：

```bash
python -m agent_core.skillhub update web-expert
```

注意：`update` 会把 `status` 重置回 `experimental`——内容已变，旧的评审不再覆盖它；
需要重新 `verify --mark-verified`（见下）。

## 技能上线流程（add → verify → mark-verified）

```bash
# 1. 登记：扫描源入 catalog（status=experimental，防误装未检验的技能）
python -m agent_core.skillhub add web-expert

# 2. 评审/完整性核对：重算源 hash 对比 catalog
python -m agent_core.skillhub verify web-expert        # 全绿 → 可评审内容是否合格

# 3. 标记 verified（人工/安全评审通过后才执行）
python -m agent_core.skillhub verify web-expert --mark-verified
```

`status` 取值：`verified`（检验过，默认放行）/ `experimental`（未检验）/ `deprecated`（下架，绝不安装）。

## 新节点安装

```bash
# 一句话装「检验过」的技能到节点根
python -m agent_core.skillhub install --target D:/nodes/node-a

# 需要 MCP 配置时再附一张表
python -m agent_core.skillhub install --target D:/nodes/node-a --with-mcp
```

- 装进 `<target>/workspace/agent/skills/`；hash 不符的源**拒绝**并不落盘。
- 已装且 hash 相等 → 幂等跳过；目标文件缺失/被改 → 补齐/覆盖回 catalog 版本。
- `--name a,b` 只装指定技能；`--include-experimental` 放行 experimental（完整性硬门仍生效）；
  `--dry-run` 只报告不写盘。
- `--with-mcp` 把可用的 MCP 条目生成到
  `<target>/workspace/agent/skillhub_runtime/mcp.json`，新节点把
  `ORCH_MCP_CONFIG` 指向该文件即接线。

### 清单对账

```bash
python -m agent_core.skillhub list            # 技能 × 源存在 × hash 对账 + MCP 服务器
python -m agent_core.skillhub list --status experimental
```

## MCP 登记到 catalog

`mcp_servers` 条目格式（示例，密钥**绝不入 catalog**，用 `${VAR}` 占位）：

```json
{ "name": "orchestrator",
  "status": "verified",
  "kind": "command",
  "config": { "command": "python", "args": ["-m", "mcp_server"] },
  "env_vars": [
    { "name": "ORCH_API_KEY", "required": true, "description": "大脑 API 密钥", "default": "" }
  ] }
```

- `kind`: `command`（本地进程 MCP）或 `url`（远程 HTTP/SSE MCP）；`config` 内任意字符串值都可写
  `${VAR}` 占位。
- `env_vars`: 声明条目所需环境变量。`required: true` 且环境（与 `default`）都取不到 →
  **install 跳过该条目并告警**（宁缺不装缺密钥的 MCP）；取得到 → 用实值替换 `${VAR}`。
- `default` 给可选变量一个缺省兜底。
- 未声明的 `${VAR}` 按 `os.environ` 解析，取不到则原样保留（可选语义）。

新节点接线：`install --with-mcp` 生成 mcp.json → 节点 `ORCH_MCP_CONFIG=<target>/workspace/agent/skillhub_runtime/mcp.json`。

## Worker 启动自动同步（可选）

节点 worker 启动时对标技能（只修技能、不碰 MCP 文件，避免覆盖运维手改的配置）：

```bash
# 节点 worker 环境
ORCH_SKILLHUB_AUTO=1
```

启动时 `auto_sync` 把本仓库 catalog 里 verified 技能对账到自身 `workspace/agent/skills/`，
失败（如源漂移未 update）**仅告警不阻断启动**，日志列出 refused/mismatch。

## catalog 结构

```json
{ "schema_version": 1,
  "skills": [ { "name", "source": "../skills/<name>", "version", "status",
                "description", "requires_mcp": [], "files": { "relpath": "sha256" } } ],
  "mcp_servers": [ { "name", "status", "kind": "command|url", "config": {...},
                     "env_vars": [ { "name", "required", "description", "default" } ] } ] }
```

由 `skillhub add/verify/update` 维护，不建议手改；`files` 的 relpath 一律为 posix 风格，
`__pycache__`/缓存目录不计入 hash。