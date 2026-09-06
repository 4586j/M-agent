# 高质量 Agent 项目搜集（2026-09）

> 搜集标准：① 权威维护方或大规模社区验证；② 生产采用案例；③ 对本项目（多 agent
> 编排框架）有明确架构借鉴价值；④ 活跃维护、许可开放。每个条目都给出
> 「对本项目的借鉴点」——不做清单搬运，只收能学到东西的。
> Star 数为搜索资料中的近似值（2026-09 时点），仅供参考。

---

## 1. 编排框架（与本项目同类，重点对标）

### LangGraph（LangChain）★ 首选精读
- **定位**：低层有状态编排框架，图结构 + 持久化 + durable execution。
- **质量信号**：2026 年生产选型共识第一；LangChain 官方维护；自带 Checkpointer/human-in-the-loop。
- **借鉴点**：咱们的 `task_store.py` 状态机是自研 JSON 版——LangGraph 的 **checkpoint 设计**（每步持久化 + 断点恢复 + 时间旅行）是成熟参照；它的 `Store` 记忆接口（namespace+key）与我们 memory_store 的分层可以互印证。

### AutoGen（Microsoft）
- **定位**：对话式多 agent 模式开创者，Azure 生态亲和。
- **借鉴点**：**对话式协作模式**（群聊、辩论、双人对话）是我们"大脑派发"之外缺少的第二种编排形态；其 AgentChat 高层 API 与底层核心分离的分层值得抄。

### CrewAI
- **定位**：角色化团队工作流，上手最快。
- **借鉴点**：**角色定义的工程化**（role/goal/backstory → 派发 prompt 模板）与我们的 executor.md/security-guard.md 同构，看它如何把角色约束做成可复用资产。

### OpenAI Agents SDK / Google ADK
- **定位**：两大厂商官方 SDK，handoffs（agent 间移交）与多 agent 会话原语。
- **借鉴点**：**handoffs 语义**——worker 间移交任务的标准接口设计，比我们自定义 HTTP 协议更通用。

## 2. 国内生态（中文资料友好，工程质量高）

### AgentScope 2.0（阿里通义实验室）★ 生产化程度最高
- **定位**：production-ready 多 agent 框架，拖拽工作流 + 沙箱运行时 + 可观测性。
- **质量信号**：2026-05 发布 2.0；有 arXiv 论文（2508.16279）；"美国生态之外最 production-minded"的评价。
- **借鉴点**：**沙箱化运行时**与**运行时管理（Studio 可视化）**直接对应我们 worker 执行隔离与 Web 面板的演进方向；其显式消息传递模型值得对照派发器抽象。

### MetaGPT（FoundationAgents）★ SOP 流水线鼻祖
- **定位**：模拟软件公司的多角色协作（产品经理→架构师→工程师→QA），ICLR 2024。
- **借鉴点**：**SOP 固化为角色流水线**正是我们 web-expert/back-expert 流程型技能的同类设计；看它如何定义角色间**产物契约**（文档格式即接口）， ours 的 `01-spec.md → 05-tasks.md` 可借鉴其结构化产物校验。MGX 是其产品化形态。

### Qwen-Agent（阿里）
- **定位**：Qwen 官方 agent 框架，函数调用/代码解释器/浏览器助手。
- **借鉴点**：**以模型能力为中心的轻量抽象**（不重编排重工具）；其内置应用的 prompt 设计是中文场景范本。

### Eino（字节跳动 CloudWeGo）
- **定位**：Go 生态的 LLM 应用框架（"Go 版 LangChain"），约 11.5K star。
- **借鉴点**：**组件抽象的完备性**（ChatModel/Tool/Retriever/Memory 各自成层）；字节内部半年迭代的工程化（强类型编排、流式处理）适合作为 API 设计参考。若未来 worker 需要高并发 Go 实现，直接复用。

## 3. 记忆系统（我们已自研轻量版，重点看演进路线）

### Letta（MemGPT）★ 与我们架构最同构
- **定位**：OS 式分层记忆（core/archival/recall）+ 自编辑记忆的 agent 框架，Apache 2.0。
- **借鉴点**：我们的 MemoryStore 三层（经验/上下文/程序记忆）刚接线完成，Letta 的 **memory blocks 共享机制**（同一 block 挂多个 agent）是实现"团队共享记忆"的现成范本；其**记忆使用统计**（我们刚加的 hits/last_hit_at 落盘）在 Letta 里如何驱动 eviction 值得对照。

### Mem0
- **定位**：通用记忆层（混合向量+图），Apache 2.0，LoCoMo 基准 91.6%（厂商口径，有争议）。
- **借鉴点**：**记忆操作抽象**（ADD/UPDATE/DELETE/NOOP 决策）——我们经验库写入是"每任务必记"，Mem0 的"先决策要不要记"更省噪声；我们的经验淘汰机制可借鉴其置信度/衰减思路。

### Zep / Graphiti
- **定位**：时序知识图谱记忆（Graphiti 为 OSS 引擎）。
- **借鉴点**：**时间有效性建模**（事实带 valid_at/expired_at）——我们的知识库积累池（tokens.json/registry.md）目前无版本与时效管理，Graphiti 的时序边设计是升级路线。

### Cognee
- **定位**：记忆管线（数据→知识图谱+向量混合），新兴但架构清晰。
- **借鉴点**：**ECL 管线**（Extract-Cognify-Load）把知识库建设流程化，对应我们 web-expert knowledge/ 积累池的自动化入库想象。

## 4. 自主执行体（executor 角色的天花板参照）

### OpenHands（All Hands AI，原 OpenDevin）★ MIT
- **定位**：自主编码 agent 平台 + Software Agent SDK（Python/TS/REST），CodeAct 2.1 SOTA。
- **借鉴点**：**事件流架构**（所有 agent 动作可审计回放）——我们 worker 的 TaskResult 只留终态，事件流是排障与评估的基础设施；其 Agent SDK 的**运行时隔离**（容器沙箱执行）是 worker 安全模式的进阶参照（我们目前仅进程级隔离 + bypassPermissions 风险收口）。

### SWE-agent（Princeton NLP）
- **定位**：学术界的 ACI（Agent-Computer Interface）标杆，ICLR 2024。
- **借鉴点**：**ACI 设计方法论**——工具接口按"模型易用性"优化（lint-gated edit 工具等），直接适用于我们 executor.md 工具约定的打磨；论文中"工具设计花的时间超过 prompt"的量化结论与我们的调研互证。

### Open SWE（LangChain）
- **定位**：开源内部编码 agent 框架（plan/apply/review 三阶段）。
- **借鉴点**：**plan-apply-review 分离**与我们大脑七阶段同构，其 LangGraph 实现是"编排框架落地自家 agent"的工程参考。

## 5. 协议与互操作（跨主机演进的标准化路线）

### MCP（Anthropic）
- **定位**：agent↔工具/数据层标准，事实行业标配（Anthropic/OpenAI/Google/MS 共同支持）。
- **借鉴点**：我们的 worker HTTP 协议（/task /task/{id} /artifact）是私有接口；工具接入侧直接采用 MCP 可让两个资产包的 `tools/mcp-setup.md` 设想落地。**注意反面教训**：GitHub MCP 90+ 工具耗 46k token——工具面要收敛。

### A2A（Google，2026-08 入 Linux Foundation AAIF）
- **定位**：agent↔agent 层标准（任务委托、Agent Card 发现），协议战争基本收敛。
- **借鉴点**：**Agent Card = 我们 /register 的 capabilities 字段的标准版**。跨主机协议若要开放给第三方 worker，A2A 是现成迁移路径；brain 可做"A2A 网关"。

### ACP / UCP
- **定位**：另外两个互补协议（ACP 偏 agent 通信、UCP 偏商业动作）。
- **借鉴点**：了解即可，当前 MCP+A2A 双层（工具层+协作层）是 2026 主流架构。

## 6. 评估与可观测（我们的空白区）

### Arize Phoenix ★ 开源首选
- **定位**：开源 AI 可观测+评估平台（OpenTelemetry 基座，LLM-as-judge 内置），Apache 2.0 可自托管。
- **借鉴点**：给 worker SDK 会话加 OTel trace 上报（我们已采 usage/cost/duration 到 meta，向 Phoenix 导出是自然延伸）；trace 回放支撑程序记忆演化的证据链。

### Langfuse（MIT）
- **定位**：追踪+数据集+实验+评估闭环，开源可自托管。
- **借鉴点**：**prompt 管理 + 数据集评估**闭环对应我们的经验库与程序记忆演化——把每次任务的 prompt/输出存数据集，改 SKILL.md 前后跑对比，就是我们演化流程缺的"回归测试"。

### AgentBench（清华 THUDM）/ General AgentBench
- **定位**：LLM-as-Agent 综合基准（8 环境，ICLR 2024，1200+ 引用）。
- **借鉴点**：部署前模型选型依据（haiku/sonnet 路由策略需要基准支撑）；不直接集成，作为选型参考。

---

## 借鉴优先级建议（结合本项目现状）

| 优先级 | 项目 | 提取什么 | 落到哪 |
|---|---|---|---|
| P0 | LangGraph | checkpoint/断点恢复设计 | task_store 演进 |
| P0 | Letta | memory blocks 共享 + 记忆统计驱动淘汰 | MemoryStore 团队记忆 |
| P1 | OpenHands | 事件流审计、容器沙箱隔离 | worker 可观测与安全 |
| P1 | SWE-agent | ACI 工具设计方法论 | executor.md 工具约定打磨 |
| P1 | Phoenix/Langfuse | trace 上报 + 数据集回归评估 | 程序记忆演化的证据闭环 |
| P2 | A2A | Agent Card 标准化 /register | 跨主机协议开放化 |
| P2 | Zep/Graphiti | 知识时效建模 | knowledge/ 积累池版本管理 |
| P2 | AgentScope 2.0 | 沙箱运行时 + Studio 可视化 | Web 面板与 worker 运行时 |

## 阅读顺序建议

1. **先读论文/设计文档**：SWE-agent ACI 论文（方法论）→ MemGPT 论文（记忆分层）→ Anthropic 三篇工程博客（已有调研报告）
2. **再精读代码**：LangGraph（checkpoint）→ Letta（memory blocks）→ OpenHands SDK（事件流）
3. **按需集成**：Phoenix（观测）→ MCP（工具标准化）→ A2A（协议开放）

---

## 附：主要来源

- LangChain 官方框架对比 — https://www.langchain.com/resources/ai-agent-frameworks
- AgentScope — https://github.com/agentscope-ai/agentscope 、https://agentscope.io/ 、arXiv 2508.16279
- OpenHands — https://github.com/OpenHands/openhands 、Software Agent SDK（arXiv 2511.03690）
- 记忆系统对比 — https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared 、https://vectorize.io/articles/best-ai-agent-memory-systems
- 协议生态 — https://zylos.ai/research/2026-02-15-agent-to-agent-communication-protocols/ 、arXiv 2505.02279
- Eino — https://github.com/cloudwego/eino ；Qwen-Agent — https://github.com/QwenLM/Qwen-Agent
- MetaGPT — https://github.com/foundationagents/metagpt
- Phoenix — https://github.com/arize-ai/phoenix ；Langfuse — https://langfuse.com/ ；AgentBench — https://github.com/THUDM/AgentBench
