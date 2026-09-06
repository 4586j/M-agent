# Agent 开发调研：架构设计、输入输出、知识库与记忆管理

> 调研日期：2026-09-07。来源以一手工程资料为主（Anthropic 工程博客 ×3、Manus 实战总结、
> Letta/MemGPT 与 LangGraph 官方文档、Redis 知识图谱 RAG 指南），辅以学术综述检索。
> 第 6 节给出对本项目（多 agent 编排框架）的映射与建议。

---

## 1. 架构设计：Workflow 与 Agent 的谱系

### 1.1 基本定义（Anthropic《Building Effective Agents》）

- **Workflow**：LLM 与工具按**预定义代码路径**编排——可预测、一致、低成本。
- **Agent**：LLM **动态决定自己的流程与工具使用**——灵活但延迟/成本/错误累积都更高。

核心建议：**从最简方案起步**。很多场景优化单次 LLM 调用（检索 + 上下文示例）就够；任务固定用 workflow，需要模型驱动决策才用 agent。框架能加速起步但增加抽象层，必须理解底层。

### 1.2 五种工作流模式

| 模式 | 机制 | 适用场景 |
|---|---|---|
| Prompt Chaining | 固定顺序子任务，可插程序化"门"检查 | 可干净分解的任务，用延迟换准确率 |
| Routing | 先分类输入再分流到专门处理 | 类别清晰（客服分流、按难度选模型） |
| Parallelization | sectioning（独立子任务并行）/ voting（同任务多次投票） | 护栏与主逻辑分离、多重审查 |
| **Orchestrator-Workers** | 中央 LLM 动态拆解、派发、汇总；**子任务不预设** | 多文件代码修改、复杂研究——"黄金模式" |
| Evaluator-Optimizer | 一个生成、一个评估循环反馈 | 有清晰评估标准且迭代有价值（翻译、搜索） |

### 1.3 Orchestrator-Workers 的生产经验（Anthropic 多 agent 研究系统）

- **Token 经济学**：单 agent 约耗普通聊天 4× token，多 agent 约 **15×**；token 用量一项解释了 80% 的性能方差。适用条件：高价值、可并行、信息量超单窗口；**不适用**：需共享上下文/强相互依赖的任务（多数编码任务）。
- **并行两层叠加**：lead 同时启动 3–5 个 subagent × 每个 subagent 单轮并行多工具调用，研究时间最多缩短 90%。
- **同步 vs 异步**：当前同步（lead 等全部完成）协调简单但 lead 无法中途纠偏、慢 subagent 阻塞全局；异步是方向但带来结果协调/状态一致性/错误传播难题。
- **Subagent 任务指令四要素**：目标、输出格式、该用的工具与来源、**明确任务边界**。模糊指令（"研究半导体短缺"）导致分工重叠失效。
- **按复杂度分配算力**（写进 prompt 的硬规则）：简单事实 = 1 agent/3–10 次调用；对比类 = 2–4 subagent/各 10–15 次；复杂研究 = 10+ subagent。
- **终态评估范式**：改持久状态的 agent 不逐轮验证中间步骤，而是检查**最终状态**是否正确 + 离散检查点，允许不同路径达成同一目标；LLM-as-judge 用单次调用、0.0–1.0 打分 + pass/fail 一致性最好；约 20 个真实样本的小评测集足以看清早期改动效果。

### 1.4 自主 Agent 设计要点

- Agent 本质 = "LLM 在循环中根据环境反馈使用工具"；每步从环境取 ground truth（工具结果、代码执行）评估进度。
- 设停止条件（最大迭代数）、检查点、受阻时暂停寻求人类反馈。
- **ACI（Agent-Computer Interface）**：投入与 HCI 同等的精力——工具文档像写给初级开发者的 docstring；SWE-bench 实践中花在**工具优化**上的时间超过 prompt。
- 三原则：保持简单；显式展示规划保持透明；精心打造 ACI。

---

## 2. 输入管理：上下文工程（Context Engineering）

### 2.1 第一性原理：注意力预算

- LLM 有有限的"注意力预算"，每个 token 都消耗它；**Context Rot**：上下文越长，召回精度越弱（根源是 n² 注意力摊薄 + 长序列训练数据稀少）。
- 指导原则：**用最小的高信号 token 集合，最大化目标行为概率**。上下文是边际收益递减的稀缺资源。
- 行业共识的五类操作：**offload（外置）、reduce（压缩）、retrieve（按需取）、isolate（隔离到子代理）、cache（缓存）**。

### 2.2 长任务三技术（Anthropic）

1. **Compaction（压缩）**：接近窗口上限时做高保真摘要并重开窗口。保留架构决策/未解决 bug/实现细节，丢弃冗余工具输出；最轻量形式是清掉历史中的工具调用结果。先保召回率再提精确率。
2. **结构化笔记（Agentic Memory）**：agent 定期把笔记写到窗口外持久存储（NOTES.md、todo），之后读回。案例：Claude 玩宝可梦在数千步中靠笔记追踪进度，上下文重置后继续任务。
3. **子代理架构（隔离）**：主代理协调，子代理用干净窗口深潜（可烧数万 token），只回传 1–2k token 精炼摘要。

### 2.3 Just-in-Time 检索（混合模式最佳）

- agent 只持有**轻量标识**（文件路径、查询、链接），运行时按需加载数据，而非预载全部。Claude Code 即此模式（Bash head/tail 分析大文件不载全量）。
- 元数据本身是信号：文件名、目录层级、时间戳构成渐进式披露。
- 权衡：运行时探索慢且可能走死胡同 → 最佳实践是**混合**：部分预取（CLAUDE.md），部分自主探索（glob/grep）。

### 2.4 KV-cache 工程（Manus 实战）

- **KV-cache 命中率是生产 agent 第一指标**：agent 输入/输出比约 100:1，缓存命中可省 10 倍成本并大幅降首 token 延迟。
- 做法：prompt 前缀稳定（系统提示开头不放时间戳）；上下文**追加式**不回改；序列化确定（JSON key 顺序稳定）；显式缓存断点。
- **掩码工具而非增删工具**：动态改工具集会使缓存全失效并诱发幻觉动作；用状态机 + logit 掩码控制可用性，工具命名加统一前缀（browser_/shell_）便于按组约束。

### 2.5 系统提示与示例

- 用"正确海拔"的语言：避免脆弱的硬编码 if-else，也避免空泛指导；XML/Markdown 分节；追求"完整描述预期行为的最小信息集"。
- 示例要**精选多样、典型**，不堆砌边缘情况——"examples are the pictures worth a thousand words"。

---

## 3. 输出管理

- **输入输出不对称是根本约束**：每步追加动作+观测使上下文膨胀，而输出（函数调用）很短。一切缓存/压缩/成本策略由此推导。
- **结构化输出是第五支柱**：强制 schema（JSON/函数调用）保证一致、可解析、可行动。关键裁决（如安全检验）用 schema 强制返回 `{verdict, reason, matched_rules, suggestion}` 结构，零解析风险。
- **保留错误**："leave the wrong turns in the context"——擦除失败等于擦除证据；模型看到失败动作和堆栈后会调低重复同类动作的先验。清理轨迹重试 = 靠温度赌运气，模型学不到东西。错误恢复是真正 agentic 行为的标志。
- **防 few-shot 死循环**：上下文里大量相似"动作-观测对"会让模型机械跟风、漂移甚至幻觉；注入受控多样性（措辞/格式/顺序的轻微变化）。"上下文越均质，agent 越脆弱。"
- **产物外置 + 轻量回传**：subagent 把代码/报告写入外部存储（文件系统），只给协调者回传路径与摘要——防止多级"传话游戏"信息失真，也省 token。
- **复述操纵注意力**：反复重写 todo 并放到上下文末尾，把全局计划推入近期注意力（对抗 lost-in-the-middle）；Manus 平均任务 50 次工具调用全靠这机制对齐。

---

## 4. 知识库管理

### 4.1 向量 RAG 的三类盲区

1. **关系路径查询**（多跳）：答案分散在由"关系"连接的多个文档中（工单→修复版本→issue），相似度检索取回"都像但都不答"的块。
2. **全库性问题**："工单前五大主题"是整体性质，没有单个段落与之相似。
3. **稠密互联领域**：代码依赖、引用网络里什么都像什么都，近邻全是 near-miss，越检索越噪。

### 4.2 GraphRAG 与混合检索

- GraphRAG 用 LLM 从非结构化文本抽**实体+关系**建图，索引单元是节点/边而非 chunk。四步：数据抽取 → 查询实体链接 → 图遍历（Cypher）→ 响应生成。
- 多跳数据集（MuSiQue/HotpotQA 等）上图方法准确率高出密集检索基线 **36%**；只取每跳所需证据也减少幻觉（**过度检索反而增加幻觉**）。
- 企业最佳实践是**混合检索**：向量（语义）+ 关键词（精确）+ 图遍历（关系）三者互补。
- 工具面控制：直接 text-to-SQL 不可靠、把几百个 API 转成几百个 MCP 工具会烧爆上下文（GitHub MCP server 90+ 工具耗 46k token）——应收敛为少量语义化检索工具。

### 4.3 新鲜度与治理（生产最常见的静默失败）

- 批量索引导致图**静默过期**：过期 API 文档的向量可以和最新文档打分一样高；版本敏感问题上标准 GraphRAG 仅 64% 准确率。
- 失败模式：陈旧关系、实体衰减、版本盲区；多 agent 共享知识时还有一致性问题（一个 agent 更新、另一个读旧值）。
- 修复方向：CDC（变更数据捕获）+ 增量近实时更新替代周期性全量重建；知识条目带版本与时间元数据。
- 人在环的**策展**对受监管领域关键：权威、人工审核的知识源 + agent 检索的动态补充。

---

## 5. 记忆管理

### 5.1 分类学（CoALA 框架，业界通用）

| 类型 | 内容 | agent 对应物 |
|---|---|---|
| Working（工作记忆） | 当前会话即时状态 | 上下文窗口本身 |
| Episodic（情景记忆） | 过往事件与经历 | 历史任务轨迹、少样本示例 |
| Semantic（语义记忆） | 事实与概念 | 用户/领域知识、世界模型 |
| Procedural（程序记忆） | 做事的规则 | 系统提示、代码、技能定义 |

工程上另一常用二分：**短期**（窗口内/checkpoint）vs **长期**（向量库/结构化库持久化）。

### 5.2 Letta（MemGPT）：OS 式分层 + 自编辑

- **Core memory**：memory blocks 固定钉在系统提示（in-context, pinned）；**Archival/Recall**：窗口外的持久库，驱逐后仍可检索取回。
- **Self-editing memory**：agent 通过工具修改自己的记忆（含核心块）；开发者也可经 API 直接编辑。
- **Shared blocks**：同一 memory block 可 attach 到**多个 agent**——实现共享"团队记忆"。
- 全部状态入数据库，窗口驱逐 ≠ 丢失；上下文管理（eviction）由系统自动处理。

### 5.3 LangGraph：三类记忆的实现范式

- **Semantic**：Profile（单文档持续更新，全但易错）vs Collection（多条窄文档，召回好但需管理增删）；存于 store，支持语义搜索 + 过滤。
- **Episodic**：存过去输入-输出对，作**少样本示例**注入 prompt；难点是按当前输入选最相关示例。
- **Procedural**：最常见实践是 agent **自我修订系统提示**（Reflection 元提示：旧指令 + 近期对话/反馈 → 新指令）。
- **写入时机**：hot path（实时写，低延迟但占推理）vs background（异步任务，不影响主流程但需设计触发频率）。
- **存储结构**：namespace（类文件夹）+ key（类文件名）的层级化 JSON 文档。

### 5.4 写入门控与安全

- 写入治理决定记忆质量：任意写入会积累噪声与错误信念。门控方式包括评估器判定、多方共识投票（本项目已实现三方共识）、只写成功经验等。
- 学术界提出 **mnemonic sovereignty（记忆主权）**：系统对"什么可写入、谁可写入、可恢复"的可验证治理——长期记忆是攻击面（提示注入持久化）。
- 遗忘机制与保留同等重要：TTL、淘汰上限、归档清理；否则检索噪声逐年上升。

---

## 6. 对本项目的映射与建议

对照 `D:\agent\agent` 多 agent 编排框架现状：

### 6.1 已对齐业界的设计（保持并发扬）

| 业界实践 | 本项目对应 |
|---|---|
| Orchestrator-Workers 黄金模式 | 大脑 + executor/security-guard，ORCH_MODE 切换单机/跨主机 |
| 结构化裁决输出 | security-guard 强制 `{verdict, reason, matched_rules, suggestion}` schema |
| 产物外置 + 轻量回传 | artifacts 落 task 工作目录，回传路径清单 + 摘要 |
| Episodic 记忆自动注入 | ExperienceStore 检索 top-3 成功经验拼入 context_memory |
| 保留错误 | 任务失败记录 verdict/error 进经验库 |
| 写入门控（超前） | 核心记忆三方共识投票——比多数业界实现严格 |
| 结构化笔记 | 技能 knowledge/ 积累池 + P9 积累义务 + 复盘三问 |
| 按复杂度分配算力 | 大脑 SKILL.md 的 model 覆盖规则 |

### 6.2 主要缺口与建议（按优先级，2026-09-07 起落地）

1. **P0 — 记忆接线** ✅ 已实现：MemoryStore 接入 brain_api——`/memory/core`（核心记忆 CRUD，写入门控仍由大脑流程负责）与 `/memory/context/{task_id}`（上下文笔记读写/归档）端点；任务终态自动写 result 笔记进上下文记忆（Remote/Async Dispatcher 共享 write_result_note）；`/dispatch` 新增 `context_task_ids` 引用历史任务上下文。
2. **P0 — 经验库淘汰机制** ✅ 已实现：`ORCH_EXPERIENCE_MAX`（默认 500）容量上限，淘汰序 = 失败经验最旧先出 → 成功经验按命中次数（内存计数，检索命中即保护）→ created_at 旧先出；`ORCH_EXPERIENCE_TTL_DAYS` 可选 TTL。
3. **P1 — Compaction** ✅ 最轻量形式已实现：`ORCH_CONTEXT_MEMORY_MAX`（默认 8000 字符）对派发 context_memory 限幅，截断保头部并标注；经验注入本就截断（prompt 100 字/摘要 200 字）。SDK 会话内压缩受 SDK 限制，max_turns=50 仍是唯一护栏。
4. **P1 — KV-cache 友好的 prompt 前缀** ✅ 已实现：worker 的 system prompt 只含跨任务稳定内容（executor.md + 技能环境块），任务工作目录移入用户消息开头——system prompt 前缀可跨任务复用。
5. **P1 — 混合检索** ✅ 已实现：ExperienceStore.search 关键词与语义两路用 Reciprocal Rank Fusion 融合（分数尺度无关、结果确定）；无 embedding 记录仍可经关键词通道召回（旧版会被向量检索完全丢弃）。
6. **P2 — Procedural 记忆演化** ✅ 已实现：ProceduralStore「提案-共识-应用」——反思报告端点（近期经验确定性聚合）→ 提案（目标限 skills/ 与 .claude/agents/ 下既有文件，security 视角由规则引擎创建时自动裁决）→ brain/executor 投票 → 三票全 pass 自动写回；hash 乐观锁防目标漂移，原内容随提案归档。流程已写入大脑 SKILL.md 记忆层。
7. **P2 — 经验条目的时效元数据** ✅ 已实现：hits/last_hit_at 随检索命中持久化落盘（淘汰保护跨重启生效），version 字段随显式 write 递增（命中计数不递增），供后续一致性检查。

---

## 附：主要来源

- Anthropic, *Building Effective Agents* — https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, *Effective Context Engineering for AI Agents* — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, *How we built our multi-agent research system* — https://www.anthropic.com/engineering/multi-agent-research-system
- Manus, *Context Engineering for AI Agents: Lessons from Building Manus* — https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Letta Docs, *Memory* — https://docs.letta.com/guides/agents/memory
- LangChain Docs, *LangGraph Memory*（semantic/episodic/procedural）— https://docs.langchain.com/oss/python/langgraph/memory
- Redis, *Knowledge Graph RAG: structured retrieval for AI agents* — https://redis.io/blog/knowledge-graph-rag-structured-retrieval-ai-agents/
- Zhang et al., *A Survey on the Memory Mechanism of LLM-based Agents*（ACM 2025）— https://dl.acm.org/doi/10.1145/3748302
- *A Survey on the Security of Long-Term Memory in LLM Agents*（arXiv）— https://arxiv.org/html/2604.16548v1
- IAAR-Shanghai, *Awesome-AI-Memory* — https://github.com/IAAR-Shanghai/Awesome-AI-Memory
