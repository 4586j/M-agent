---
name: back-expert
version: 1.0.0
description: >
  后端服务开发专家 Skill。把"从需求到上线服务"固化为六阶段流水线
  （S0 需求→spec → S1 领域建模与架构 → S2 数据与契约设计 → S3 任务拆解+开发 → S4 测试联调走查 → S5 上线复盘），
  每阶段有退出卡点（G1~G5），契约冻结后即前后端/服务间的法律合同（API-First），
  开发资产沉淀在 knowledge/ 积累池供跨任务复用。
author: back-expert-team
tags:
  - backend
  - api
  - api-first
  - spec-driven
  - pipeline
triggers:
  - 做一个服务
  - 后端开发
  - 新接口
  - 契约设计
  - 数据库设计
  - 后端任务
---

# back-expert：后端服务开发工作流

## 本技能是什么

本目录是「后端开发工作流」资产包在多 agent 框架中的实例（源资产包：`D:\agent\back`，历史任务产物与 git 历史留在源仓库，未随迁）。与 web-expert 同为**流程型技能**：产出由 executor 按六阶段流水线逐步完成，框架的价值在于**按阶段派发、按卡点拦截、按积累规则沉淀**。

## 入口与必读（executor 接到后端任务后按序阅读）

1. `AGENTS.md` —— 本资产包的 AI 工作约定（标准动作、积累规则、硬性约束）
2. `workflow/pipeline.md` —— 六阶段流水线 S0~S5 定义，先判断任务处于哪个阶段
3. `workflow/constitution.md` —— 11 条宪法原则（P1 单一事实源 / P6 安全底线 / P7 性能预算 / P8 幂等一致性 / P9 积累义务 / P10 偏移即停 / P11 调研先行等），冲突时按宪法裁决
4. `workflow/gates.md` —— G1~G5 卡点验收清单，未全过不得进入下一阶段
5. 阶段资产（按需）：
   - `knowledge/conventions/response-and-errors.json` —— 响应包裹/错误码/命名/分页唯一事实源，禁止临时发明错误码
   - `knowledge/components/registry.md` —— 已登记模块（四层分层），写代码前先查，分层依赖只上不下
   - `knowledge/patterns/api-patterns.md` —— API/后端模式库，设计前先查、走查时核对常见坑
   - `knowledge/research/` —— 既有调研笔记，避免重复检索
   - `knowledge/prompts/` —— spec 生成、契约生成、任务拆解等模板
   - `knowledge/checklists/backend-qa.md` —— 后端走查清单（安全/性能/一致性/可观测/契约/可维护，S4 用）

## 框架对接（大脑派发时必须遵守）

### 派发 prompt 模板（大脑阶段⑥生成）

```
[back-expert] 阶段：S<0-5>；输入：<上阶段产物路径>；任务：<该阶段工作，见 pipeline.md>；
退出卡点：G<对应卡点>；产出写到：output/<项目名>/
```

- **产物位置**：`workspace/agent/skills/back-expert/output/<项目名>/`，标准文件名：`01-spec.md`、`02-架构说明.md`、`03-契约定义.md`、`04-走查报告.md`、`05-tasks.md` + 服务代码
- **阶段即子任务**：技能工作流内的每个阶段（S0~S5）是一次 executor 派发；大脑对照 `gates.md` 确认卡点通过后才派发下一阶段；多阶段合并派发仅限低风险相邻阶段，且需在任务记录中说明
- **契约冻结（API-First）**：S2 产出的 `03-契约定义.md` 通过 G2b 后即为前后端/服务间的法律合同，变更必须走变更记录；前端联调以契约为准
- **跳阶段**：允许但必须记录原因与契约由谁提供（pipeline.md 首部规则），如已有明确接口契约从 S3 进入
- **卡点 ↔ 安全关卡**：G1~G5 是业务卡点（大脑/executor 对照 gates.md 自查），与 security-guard 的关卡①②**正交，两者都要过**；security-guard block 时仍按大脑硬约束停给用户
- **积累回写（P9 积累义务）**：S5 收尾按 `AGENTS.md` 第 4 节把模块/模式/调研/复盘回写 `knowledge/` 对应池，**未回写 = 任务未完成**；大脑阶段⑦审核时检查此项，未回写退回 executor 补做
- **调研先行（P11）**：S0/S1/S2 产出前用 WebSearch 检索同类系统方案与选型证据，落盘 `knowledge/research/YYYYMMDD-主题.md`，并在 spec/架构/契约中引用
- **安全底线（宪法 P6，与 security-guard 规则叠加）**：入参必校验、查询必参数化、密钥不进仓库（只进环境变量）、日志不落敏感信息、接口必鉴权（除非明确豁免）；**AI 经 MCP 执行破坏性操作（DROP/DELETE/迁移回滚）前必须经大脑停给用户确认**，不得自行执行

## 工具

- `tools/toolchain.json` —— 机读工具清单，按 `stages` 字段过滤（什么阶段用什么工具）
- `tools/mcp-setup.md` —— 数据库/GitHub/Postman 等 MCP 接入说明
