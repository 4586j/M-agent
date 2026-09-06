# 前端页面设计制作工作流 · 资产包 v1.1

一套 **AI 可执行、可持续积累** 的前端页面设计制作工作流。它把业界主流实践（经典协作流程 + 设计系统双向桥接 + AI 生成 + 规格驱动开发）落成 AI 助手可以直接读取、执行、并不断回写的文件资产。

## 设计思路方案（为什么这样设计）

### 四个核心原则

1. **AI 可使用（可执行优先于可阅读）**
   - 流程写成"阶段 + 卡点 + 交付物"的有限状态机（`workflow/pipeline.md` + `gates.md`），AI 每次任务只需回答"我在哪个阶段、卡点过了没"。
   - 关键数据全部机读化：工具清单是 JSON（`tools/toolchain.json`），设计变量是 DTCG 格式 tokens（`knowledge/design-tokens/tokens.json`）。
   - 入口约定放在 `AGENTS.md`，任何支持 AGENTS.md 约定的 AI 编码工具（ZCode、Cursor、Claude Code 等）都会自动遵守。

2. **规格先行（Spec as Executable Contract）**
   - 吸收 GitHub Spec Kit 等 SDD 实践：`workflow/constitution.md`（宪法，10 条不可协商原则）→ S0 产出 spec（用户故事 + 可测验收标准）→ S3 前置任务拆解（MoSCoW + 每任务自带完成定义）→ 实现中**偏移检测**（发现 spec 没写的需求，停下更新 spec，禁止静默实现）。
   - spec 从"被动文档"升级为"约束 AI 产出的可执行契约"，这是 2026 年 AI 编码工作流的公认核心模式。

3. **卡点化（阶段交界处设闸门）**
   - 需求评审过不了不进设计、设计评审过不了不进开发、走查不过不上线。
   - 每个卡点 = 角色确认 + 必填信息 + 可勾选的验收清单（`workflow/gates.md`），卡点同时是宪法在各阶段的检查点实例。

4. **可积累（把一次性项目变成复利资产）**
   - 八个积累池：设计 tokens、组件登记表（原子设计四层）、**页面类型 UX 模式库**、**调研笔记库**、走查清单、提示词模板、项目复盘库、技术决策记录。
   - 积累规则强制化：任务收尾必须回写（见 `AGENTS.md` 第 4 节），只增不改历史；复盘带三个结构化反思问题（反思环）。
   - **调研先行（P11）**：设计方案产出前必须上网调研并落盘笔记（`knowledge/research/`），设计决策可追溯到外部证据。

### 一条任务的完整生命周期

```
任务进入
  ↓ AI 读 AGENTS.md（入口约定）+ constitution.md（宪法）
  ↓ S0：需求 → spec（可测验收标准；判断页面类型，查 patterns 模式库）
  ↓ S1~S2：原型与视觉（引用既有模式 + 复用登记组件 + 只用 tokens）
  ↓ S3：spec 拆解为 tasks（MoSCoW + 完成定义）→ 逐任务生成代码 → 偏移检测
  ↓ S4：visual-qa 走查 + 页面模式"常见坑"核对 → 04-走查报告
  ↓ S5：上线 + 复盘（反思三问）→ 回写积累池
任务关闭 → 下一单任务站在更高的存量上开工
```

### 与调研结论的对应关系

| 业界实践 | 本包的落地形式 |
|---|---|
| SDD：constitution → specify → plan → tasks → implement → validate（GitHub Spec Kit 等） | `workflow/constitution.md` + S0 spec 化 + S3 tasks 拆解 + 偏移日志 |
| 需求评审卡点、角色与必填信息（阿里云规范） | `workflow/gates.md` G1 |
| 原子设计 / 组件驱动设计（Atomic Design） | `knowledge/components/registry.md` 四层分组（原子/分子/组织/模板），"高层只用低层拼" |
| Design Tokens 标准化、DTCG 格式 | `knowledge/design-tokens/tokens.json` |
| Figma Dev Mode / MCP 让 AI 读懂设计稿 | `tools/mcp-setup.md` + `knowledge/prompts/page-from-design.md` |
| 页面类型 UX 模式（仪表盘/落地页/结账等，Baymard 数据支撑） | `knowledge/patterns/page-patterns.md`：核心模式 + 常见坑双清单 |
| 走查前置、像素级还原度对比（Copixel 实践） | `knowledge/checklists/visual-qa.md` + G4 卡点 |
| Vibe Coding：AI 生成 + 人工审核 | S3 阶段 AI 生成 + G3/G4 人工卡点 |
| 复盘反哺流程改进（反思环） | `knowledge/cases/` 反思三问 + 回写义务表 |

## 快速开始

1. **新项目**：把需求丢给 AI，说"按 AGENTS.md 走流程"。AI 会用 `spec-from-requirement.md` 出 spec，从 S0 走到 S5。
2. **有设计稿还原**：直接给截图或 Figma 链接，AI 从 S3 进入（仍需最小 spec），用 `page-from-design.md` 模板 + `tasks-breakdown.md` 拆任务。
3. **维护这套体系本身**：改 tokens、加组件、补模式、写复盘——都遵循 `AGENTS.md` 第 4 节的积累规则；改流程结构本身要先写 ADR。

> **交付形态（ADR-004 修订）**：博客项目为 `index.html + assets/` 目录（音频资源无法内嵌），移动或部署时需整个 `output/blog/` 目录一起带走。

## 首次使用需要替换的内容

- `knowledge/design-tokens/tokens.json` 中的默认值 → 换成你的品牌变量。
- `tools/toolchain.json` 中标注 `[可选]` 的条目 → 按团队技术栈裁剪。
- `tools/mcp-setup.md` 中的 Figma MCP 接入 → 填入你自己的 Figma 访问凭证配置。
- `workflow/constitution.md` 的 P7 性能预算等 → 按团队质量基线调整（改动需留 ADR）。

## 变更记录

- **2026-08-30 v1.2**：应用户补充，宪法新增 P11"调研先行"，S0~S2 加入调研步骤，新增 `knowledge/research/` 调研笔记积累池（决策见 `knowledge/decisions/ADR-002-design-research.md`）。
- **2026-08-30 v1.1**：吸收 Spec-Driven Development（新增 `workflow/constitution.md`、S0 spec 化、S3 任务拆解 + 偏移检测、结构化反思环）；组件登记表按 Atomic Design 分层；新增 `knowledge/patterns/page-patterns.md` 页面类型 UX 模式库；toolchain 补充 Arco/Semi 组件库与 Mobbin/Page Flows 模式参考库。决策依据见 `knowledge/decisions/ADR-001`。
- 2026-08-30 v1.0：初版六阶段流水线 + 六积累池。
