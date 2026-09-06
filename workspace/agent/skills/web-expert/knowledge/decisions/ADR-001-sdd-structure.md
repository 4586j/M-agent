# ADR-001：引入 SDD 结构（宪法 + spec + tasks + 偏移检测）与页面模式库

- 日期：2026-08-30
- 状态：accepted

## 背景

工作流 v1.0 已有六阶段流水线 + 六个积累池。第二轮调研（2026-08-30）发现两处结构性缺口：

1. **缺"可执行契约"层**。GitHub Spec Kit、Kilo Code 等 2026 年主流 AI 编码工作流收敛于 constitution → specify → plan → tasks → implement → validate 的流水线；其核心洞察是"spec 是约束 AI 产出的可执行契约，而非被动文档"，且实现阶段需要**偏移检测**（drift detection）防止 AI 静默偏离约定。v1.0 的 S0 只产出普通需求说明，S3 无任务拆解、无偏移处理规则。
2. **缺设计模式积累位**。原子设计（Atomic Design）与页面类型 UX 模式（Baymard 结账研究、仪表盘模式分析等）是成熟方法论，但 v1.0 的积累池里没有承载它们的文件，导致这类经验只能散落在复盘中。

## 决策

1. 新增 `workflow/constitution.md`：10 条不可协商原则（P1~P10），作为 spec/任务/卡点的上位依据，冲突裁决顺序：用户指令 > 宪法 > spec > 习惯。
2. S0 升级为 spec 化：产出 `01-spec.md`（用户故事 + 可测验收标准 + 非目标 + 边界 + 开放问题），配套提示词模板 `prompts/spec-from-requirement.md`。
3. S3 前置任务拆解：产出 `05-tasks.md`（MoSCoW + 每任务自带完成定义）+ 偏移日志；宪法 P10"偏移即停"。
4. 组件登记表按 Atomic Design 分四层，新增"高层只用低层拼装"规则。
5. 新增 `knowledge/patterns/page-patterns.md`：7 类页面模式库（核心模式 + 常见坑），S0/S1 引用、S4 核对。
6. 卡点同步升级：G1 spec 评审、G3 任务/偏移检查、G4 模式核对、G5 反思三问。
7. toolchain.json 补充：Mobbin/Page Flows/UI-Patterns.com（模式参考）、Arco Design/Semi Design（组件库备选）。

## 备选方案（为什么没选）

- **保持 v1.0 不动，仅靠复盘自然演化**：复盘属于事后补救，无法在实现中阻止偏移；放弃。
- **完整照搬 Spec Kit（含 /constitution、/specify 等 slash 命令体系）**：本工作流的主要执行者是对话式 AI 而非命令行工作流，命令形态不必要；只吸收结构思想，不引入其工具链。放弃。
- **模式库按页面类型一文件**：页面类型会持续增长，单文件内分节 + 变更记录更利于 AI 一次读全。放弃多文件方案。

## 后果

- 正面：spec→tasks→验证形成闭环，AI 产出可逐条追溯；设计经验有了专门积累位；流程与 2026 主流 SDD 实践对齐。
- 代价：单任务前置文档从 1 份（需求说明）变为 2 份（spec + tasks），小型任务开销增加；通过"已有设计稿时使用最小 spec"缓解。
- 影响范围：pipeline/gates/AGENTS/README 全部同步至 v1.1；后续修改宪法须先写 ADR。

## 关联来源

- [GitHub Blog：Spec-driven development with AI（Spec Kit）](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)
- [Kilo Code：Spec-Driven Development 模式（constitution→specify→plan→tasks→implement→testing）](https://path.kilo.ai/introduction/patterns/spec-driven-development/)
- [Augment Code：What Is Spec-Driven Development](https://www.augmentcode.com/guides/what-is-spec-driven-development)
- [掘金：UI 设计的底层逻辑（Atomic Design）](https://juejin.cn/post/7561995883120476199)
- [OSCHINA：设计系统落地实践（可组合性 + tokens 一致性）](https://my.oschina.net/emacs_8006037/blog/19642738)
- [Baymard Institute：Cart & Checkout Usability Research](https://baymard.com/research/checkout-usability)
- [Pencil & Paper：Data Dashboard UX Patterns](https://www.pencilandpaper.io/articles/ux-pattern-analysis-data-dashboards)
