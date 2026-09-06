# 后端开发工作流 · 资产包 v1.0

一套 **AI 可执行、可持续积累** 的后端服务开发工作流。它把业界主流实践（API-First 契约冻结 + 规格驱动开发 + CI/CD + TDD + DDD 建模）落成 AI 助手可以直接读取、执行、并不断回写的文件资产。

> 姊妹包：前端页面设计制作工作流（`D:\agent\web`）。两包共享同一套方法论骨架（宪法 → 流水线 → 卡点 → 积累池），可互相引用契约并行开发。

## 设计思路方案（为什么这样设计）

### 四个核心原则

1. **AI 可使用（可执行优先于可阅读）**
   - 流程写成"阶段 + 卡点 + 交付物"的有限状态机（`workflow/pipeline.md` + `gates.md`），AI 每次任务只需回答"我在哪个阶段、卡点过了没"。
   - 关键数据全部机读化：工具清单是 JSON（`tools/toolchain.json`），响应/错误码/命名规范是机读字典（`knowledge/conventions/response-and-errors.json`）。
   - 入口约定放在 `AGENTS.md`，任何支持 AGENTS.md 约定的 AI 编码工具都会自动遵守。

2. **规格先行 + 契约冻结（Spec & Contract as Executable Contract）**
   - 吸收 GitHub Spec Kit 等 SDD 实践：`workflow/constitution.md`（宪法，11 条不可协商原则）→ S0 产出 spec（用户故事 + 可测验收标准）→ S2 冻结契约（数据库 schema + OpenAPI 接口定义）→ S3 前置任务拆解（MoSCoW + 每任务自带完成定义）→ 实现中**偏移检测**（发现 spec 没写的需求，停下更新，禁止静默实现）。
   - 契约冻结即"法律合同"（API-First 实践）：前端可据契约做 Mock 并行开发，联调返工率显著下降。

3. **卡点化（阶段交界处设闸门）**
   - 需求评审过不了不进架构、契约评审过不了不进开发、走查不过不上线。
   - 每个卡点 = 必填信息 + 可勾选的验收清单（`workflow/gates.md`），卡点同时是宪法在各阶段的检查点实例。

4. **可积累（把一次性项目变成复利资产）**
   - 八个积累池：规范字典、模块登记表（四层分层）、**API/后端模式库**、**调研笔记库**、走查清单、提示词模板、项目复盘库、技术决策记录。
   - 积累规则强制化：任务收尾必须回写（见 `AGENTS.md` 第 4 节），只增不改历史；复盘带三个结构化反思问题（反思环）。
   - **调研先行（P11）**：设计产出前必须上网调研并落盘笔记（`knowledge/research/`），设计决策可追溯到外部证据。

### 一条任务的完整生命周期

```
任务进入
  ↓ AI 读 AGENTS.md（入口约定）+ constitution.md（宪法）
  ↓ S0：需求 → spec（可测验收标准；判断接口类型，查 patterns 模式库）
  ↓ S1：领域建模与架构（统一语言 + 架构决策 + 技术选型）
  ↓ S2：数据与契约（表结构 + OpenAPI 契约 → 冻结）
  ↓ S3：spec/契约拆解为 tasks（MoSCoW + 完成定义）→ 测试先行逐任务实现 → 偏移检测
  ↓ S4：backend-qa 走查 + 模式"常见坑"核对 + 联调 → 04-走查报告
  ↓ S5：上线 + 复盘（反思三问）→ 回写积累池
任务关闭 → 下一单任务站在更高的存量上开工
```

### 与调研结论的对应关系

| 业界实践 | 本包的落地形式 |
|---|---|
| SDD：constitution → specify → plan → tasks → implement（GitHub Spec Kit 等） | `workflow/constitution.md` + S0 spec 化 + S3 tasks 拆解 + 偏移日志 |
| API-First：契约冻结、Mock 并行、契约即文档 | S2 契约冻结（G2b）+ Prism Mock（toolchain）+ 契约变更记录 |
| DDD：统一语言、领域边界、分层防腐 | S1 领域建模 + registry 四层分层规则 + 防腐层模式（patterns 第 6 组） |
| TDD：测试先行、测试即文档 | S3 任务完成定义 = 测试命令；backend-qa 第 6 组 |
| CI/CD：自动化门禁、快速反馈 | CI 流水线即 G3/G4 检查项的自动化实例（toolchain deploy 类） |
| 输入校验/错误处理/关注点分离/安全 | 宪法 P1（统一响应）、P6（安全底线）、checklists 第 1/5 组 |
| 复盘反哺流程改进（反思环） | `knowledge/cases/` 反思三问 + 回写义务表 |

## 快速开始

1. **新服务**：把需求丢给 AI，说"按 AGENTS.md 走流程"。AI 会用 `prompts/spec-from-requirement.md` 出 spec，从 S0 走到 S5。
2. **已有契约**：直接给 OpenAPI 文件或契约文档，AI 从 S3 进入（仍需最小 spec），用 `prompts/tasks-breakdown.md` 拆任务。
3. **维护这套体系本身**：改规范、加模块、补模式、写复盘——都遵循 `AGENTS.md` 第 4 节的积累规则；改流程结构本身要先写 ADR。

## 首次使用需要替换的内容

- `knowledge/conventions/response-and-errors.json` 中的默认值 → 换成你的团队规范（错误码前缀、分页参数等）。
- `tools/toolchain.json` 中标注 `[可选]` 的条目 → 按团队技术栈裁剪（默认栈：NestJS + PostgreSQL）。
- `tools/mcp-setup.md` 中的 MCP 接入 → 填入你自己的数据库/仓库访问配置（凭证只放环境变量）。
- `workflow/constitution.md` 的 P7 性能预算等 → 按团队质量基线调整（改动需留 ADR）。

## 变更记录

- **2026-09-01 v1.0**：初版。骨架借鉴前端工作流资产包（`D:\agent\web` v1.2），内容为后端实例化：宪法 11 条（新增安全底线、幂等与一致性）、六阶段流水线（S1 领域建模、S2 契约冻结）、后端走查清单六组、API 模式库六类。调研来源：API-First / SDD（Spec Kit）/ CI/CD / TDD / DDD / Code Review 工作流。
