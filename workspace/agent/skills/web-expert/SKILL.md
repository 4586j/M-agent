---
name: web-expert
version: 1.0.0
description: >
  前端页面设计制作专家 Skill。把"从需求到上线页面"固化为六阶段流水线
  （S0 需求→spec → S1 原型 → S2 视觉设计 → S3 任务拆解+开发 → S4 走查联调 → S5 上线复盘），
  每阶段有退出卡点（G1~G5），设计/开发资产沉淀在 knowledge/ 积累池供跨任务复用。
author: web-expert-team
tags:
  - frontend
  - design
  - spec-driven
  - pipeline
  - ui
triggers:
  - 做一个页面
  - 前端开发
  - 页面设计
  - 页面还原
  - 视觉走查
  - 前端任务
---

# web-expert：前端页面设计制作工作流

## 本技能是什么

本目录是「前端页面设计制作工作流」资产包在多 agent 框架中的实例（源资产包：`D:\agent\web`，历史任务产物留在源仓库 `output/`，未随迁）。与 data-expert（代码化流水线，executor 调脚本）不同，本技能是**流程型技能**：产出由 executor 按六阶段流水线逐步完成，框架的价值在于**按阶段派发、按卡点拦截、按积累规则沉淀**。

## 入口与必读（executor 接到前端任务后按序阅读）

1. `AGENTS.md` —— 本资产包的 AI 工作约定（标准动作、积累规则、硬性约束）
2. `workflow/pipeline.md` —— 六阶段流水线 S0~S5 定义，先判断任务处于哪个阶段
3. `workflow/constitution.md` —— 11 条宪法原则（P1 单一事实源 / P2 复用优先 / P3 规格先行 / P9 积累义务 / P10 偏移即停 / P11 调研先行等），冲突时按宪法裁决
4. `workflow/gates.md` —— G1~G5 卡点验收清单，未全过不得进入下一阶段
5. 阶段资产（按需）：
   - `knowledge/design-tokens/tokens.json` —— 颜色/间距/字号唯一事实源，禁止临时发明值
   - `knowledge/components/registry.md` —— 已登记组件，写代码前先查，禁止重复实现
   - `knowledge/patterns/page-patterns.md` —— 页面类型 UX 模式库，设计前先查、走查时核对
   - `knowledge/research/` —— 既有调研笔记，避免重复检索
   - `knowledge/prompts/` —— spec 生成、任务拆解、页面还原等模板
   - `knowledge/checklists/visual-qa.md` —— 视觉走查清单（S4 用）

## 框架对接（大脑派发时必须遵守）

### 派发 prompt 模板（大脑阶段⑥生成）

```
[web-expert] 阶段：S<0-5>；输入：<上阶段产物路径>；任务：<该阶段工作，见 pipeline.md>；
退出卡点：G<对应卡点>；产出写到：output/<项目名>/
```

- **产物位置**：`workspace/agent/skills/web-expert/output/<项目名>/`，标准文件名：`01-spec.md`、`02-原型说明.md`、`03-视觉稿.md`、`04-走查报告.md`、`05-tasks.md` + 页面代码
- **阶段即子任务**：技能工作流内的每个阶段（S0~S5）是一次 executor 派发；大脑对照 `gates.md` 确认卡点通过后才派发下一阶段；多阶段合并派发仅限低风险相邻阶段（如 S1+S2），且需在任务记录中说明
- **跳阶段**：允许但必须记录原因与替代交付物（pipeline.md 首部规则），如已有高保真稿从 S3 进入
- **卡点 ↔ 安全关卡**：G1~G5 是业务卡点（大脑/executor 对照 gates.md 自查），与 security-guard 的关卡①②**正交，两者都要过**；security-guard block 时仍按大脑硬约束停给用户
- **积累回写（P9 积累义务）**：S5 收尾按 `AGENTS.md` 第 4 节把组件/模式/调研/复盘回写 `knowledge/` 对应池，**未回写 = 任务未完成**；大脑阶段⑦审核时检查此项，未回写退回 executor 补做
- **调研先行（P11）**：S0/S1/S2 产出前用 WebSearch 检索竞品与设计参考，落盘 `knowledge/research/YYYYMMDD-主题.md`，并在 spec/原型/视觉稿中引用
- **安全底线（宪法 P6/P7/P8）**：WCAG AA 可访问性（对比度 ≥4.5:1、键盘可达）、三视口 1280/768/375 不破版、性能预算

## 工具

- `tools/toolchain.json` —— 机读工具清单，按 `stage` 字段过滤（什么阶段用什么工具）
- `tools/mcp-setup.md` —— Figma MCP 等接入说明
