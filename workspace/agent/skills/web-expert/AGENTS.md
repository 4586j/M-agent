# AGENTS.md — AI 工作约定

本仓库是一套**前端页面设计制作工作流**的可执行资产包。任何 AI 助手（ZCode / Cursor / Claude 等）在本仓库工作时，必须遵守以下约定。

## 1. 这个仓库是什么

- 目标：让"从需求到一个上线页面"的全流程可以被 AI 按固定流程执行，并把每次执行的产物、经验**沉淀下来供下次复用**。
- 核心原则：
  1. **卡点化** — 每个阶段结束必须通过对应卡点（`workflow/gates.md`）才能进入下一阶段。
  2. **单一事实源** — 颜色、间距、字号只认 `knowledge/design-tokens/tokens.json`，不临时发明值。
  3. **可积累** — 每完成一次任务，必须按第 4 节规则更新积累池，否则任务不算完成。
  4. **规格先行** — 无通过评审的 spec 不开发；实现中发现 spec 没写的需求，先更新 spec 再继续（见 `workflow/constitution.md`）。

## 2. 目录结构与职责

```
AGENTS.md                 ← 你现在在读的：AI 工作入口
README.md                 ← 人类总览与设计思路
workflow/
  constitution.md         ← 项目宪法：10 条不可协商原则，spec/任务/卡点的上位依据
  pipeline.md             ← 六阶段流水线（S0~S5），接到任务从这里开始
  gates.md                ← 卡点验收清单（勾完才能过卡点）
tools/
  toolchain.json          ← 机读工具清单：什么阶段用什么工具
  mcp-setup.md            ← Figma MCP 等接入说明
knowledge/                ← 积累池（只增不改历史，持续演化）
  design-tokens/tokens.json
  components/registry.md  ← 组件登记表（原子设计分层）：写代码前先查这里
  patterns/page-patterns.md ← 页面类型 UX 模式库：设计前先查，走查时核对
  research/               ← 调研笔记库（宪法 P11：设计方案前先调研并落盘）
  checklists/visual-qa.md ← 视觉走查清单
  prompts/                ← 提示词/任务模板库（spec 生成、任务拆解、页面还原等）
  cases/                  ← 项目复盘库（TEMPLATE.md 为模板）
  decisions/              ← 技术决策记录 ADR（TEMPLATE.md 为模板）
output/                   ← 每次任务的产出（页面代码、走查记录等）
```

## 3. 接到任务时的标准动作

1. **读流程**：打开 `workflow/pipeline.md`，判断任务处于哪个阶段（S0 需求 → S5 复盘）。
2. **读资产**：开工前必读
   - `workflow/constitution.md`（11 条原则，全程约束）
   - `knowledge/design-tokens/tokens.json`（设计变量）
   - `knowledge/components/registry.md`（已有组件，避免重复造）
   - `knowledge/patterns/page-patterns.md`（若任务涉及页面设计/走查：先定页面类型）
   - `knowledge/research/`（同类项目的既有调研，避免重复检索）
   - `knowledge/prompts/` 下与任务匹配的模板
3. **设计阶段先调研（宪法 P11）**：S0/S1/S2 产出前用 WebSearch 检索竞品案例、设计模式、动效/视觉参考，发现统一落盘 `knowledge/research/YYYYMMDD-主题.md`，并在 spec/原型/视觉稿中引用要点。
4. **执行阶段工作**，产出物写入 `output/<项目名>/`；S3 必须先有 `05-tasks.md` 再写代码。
5. **过卡点**：对照 `workflow/gates.md` 逐项勾选，未全过不得进入下一阶段；走查类卡点配合 `knowledge/checklists/visual-qa.md` 与 `patterns/page-patterns.md` 的"常见坑"使用。
6. **积累回写**：按第 4 节更新积累池。

## 4. 积累规则（任务收尾必须执行）

| 情况 | 必须更新 |
|---|---|
| 新建/修改了可复用组件 | `knowledge/components/registry.md` 按原子设计层级追加或更新登记 |
| 调整了颜色/间距/字号等基础值 | `knowledge/design-tokens/tokens.json` + 同步说明 |
| 发现了可复用的页面/交互模式或新坑 | `knowledge/patterns/page-patterns.md` 追加对应页面类型的模式或坑 |
| 完成了一轮设计前调研 | `knowledge/research/` 新建 `YYYYMMDD-主题.md`（用 TEMPLATE.md） |
| 复用了或改进了提示词模板 | `knowledge/prompts/` 对应模板（改模板本体，追加变更记录） |
| 项目完成或阶段性完成 | `knowledge/cases/` 新建 `YYYYMMDD-项目名.md` 复盘 |
| 做了不可逆/影响后续的技术或结构选型 | `knowledge/decisions/` 新建 ADR |
| 走查发现了新的问题类型 | `knowledge/checklists/visual-qa.md` 增加检查项 |
| 实现中发生了需求偏移 | spec 已更新 + 复盘中说明根因（防再犯） |

积累原则：**新增条目用新编号/新文件追加；修改既有条目时保留"变更原因"一行；永不删除历史记录。**

## 5. 硬性约束

完整原则见 `workflow/constitution.md`（P1~P10），以下为最高频的强制项：

- 不使用 `tokens.json` 之外的颜色/间距/圆角值；确需新值 → 先加进 tokens 并登记变更原因。
- 不重复实现 `registry.md` 里已登记的组件；高层组件只用低层组件拼装。
- **无 spec 不开发**：S3 写代码前必须有 `01-spec.md`（过 G1）与 `05-tasks.md`；实现中发现 spec 未覆盖的需求 → 停下更新 spec 并记偏移日志，禁止静默实现。
- 工具选型优先查 `tools/toolchain.json`；要引入清单外的新工具 → 先在清单中登记（含用途、阶段、备选）。
- 每次给用户的回复要说明：当前处于流水线哪个阶段、卡点是否通过、本次沉淀了什么。

## 6. 常用任务入口

- 「做一个新页面」→ `workflow/pipeline.md` 从 S0 走起：用 `prompts/spec-from-requirement.md` 出 spec。
- 「按这张图/这个 Figma 链接还原」→ 使用 `knowledge/prompts/page-from-design.md` 模板（仍需最小 spec：页面类型 + 验收标准）。
- 「走查/验收」→ `knowledge/checklists/visual-qa.md` + `patterns/page-patterns.md` 常见坑 + `workflow/gates.md` G4。
- 「工具怎么选/怎么接」→ `tools/toolchain.json` + `tools/mcp-setup.md`。
- 「改这套工作流本身」→ 先写 ADR，再同步更新 constitution/pipeline/gates 并在各自变更记录注明。
