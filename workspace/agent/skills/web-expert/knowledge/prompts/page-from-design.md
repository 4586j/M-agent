# 提示词模板：从设计稿生成页面（S3 入口）

用途：拿到视觉稿（截图 / Figma 链接）后，让 AI 按本仓库约束生成页面代码。
使用方式：复制下方模板，替换 `{{占位符}}` 后发给 AI。模板本身可迭代——改进时直接改本文件并在"变更记录"中注明。

---

## 模板正文

```text
请在本仓库内按 AGENTS.md 约定工作，任务：还原一个页面。

【设计稿】
- 形式：{{截图附件 | Figma 链接（请通过 MCP 读取选中 Frame）}}
- 页面名称：{{页面名，如：订单列表页}}
- 范围：{{整页 | 仅 {{区块名}} 区块}}

【开工前必读】
1. knowledge/design-tokens/tokens.json —— 颜色/间距/字号只允许引用此处 token。
2. knowledge/components/registry.md —— 优先复用已登记组件，禁止重复实现。
3. workflow/pipeline.md S3 与 workflow/gates.md G3 —— 产出必须能过 G3 自测。

【技术要求】
- 栈：{{React + Vite + Tailwind（默认，可按项目替换）}}
- 目录：产出至 output/{{项目名}}/
- 响应式：完成 1280 / 768 / 375 三视口
- 状态覆盖：每个可交互元素实现 default / hover / focus / disabled / loading

【执行步骤】
1. 先列出你从设计稿中识别出的区块清单与用到的 token，等我确认（或无歧义时直接继续）。
2. 生成页面代码与缺失的新组件；新组件同步登记到 registry.md。
3. 自查 G3 卡点全部勾选项，输出自查结果。
4. 给我本地预览的运行方式。

【禁止】
- 硬编码颜色/间距/字号
- 使用 registry 中已有组件的重复实现
- 引入 toolchain.json 之外的新依赖（确需引入先说明）
```

---

## 变更记录

- 2026-08-30：初始化模板 v1（来源：调研中 Figma MCP + Vibe Coding 实践的综合）。
