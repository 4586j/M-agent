# MCP / CLI 接入说明

让 AI 助手能"读懂"设计稿与页面，是本工作流提效最大的单点投入。以下按优先级排列。

## 1. Figma Dev Mode MCP Server（强烈建议）

**作用**：AI（ZCode / Cursor / Claude）通过 MCP 直接读取当前选中设计帧的结构、布局、间距、字体、变量与组件信息，替代人工抄标注。

**接入步骤**：
1. Figma 桌面客户端（或网页版 + Professional 及以上席位）启用 Dev Mode MCP Server：
   - 桌面版：`Preferences → Enable Dev Mode MCP Server`，默认监听 `http://127.0.0.1:3845/sse`。
2. 在 AI 工具的 MCP 配置中添加（示例为通用 JSON 配置）：

```json
{
  "mcpServers": {
    "figma-dev-mode": {
      "url": "http://127.0.0.1:3845/sse"
    }
  }
}
```

3. 使用时：在 Figma 中选中目标 Frame → 在 AI 里说"读取当前选中设计稿并按 AGENTS.md 生成页面"。

**注意**：MCP 返回的是结构化设计数据（层级、auto-layout、tokens 映射），AI 仍须遵守本仓库约束——颜色/间距只引用 tokens，不照抄临时值。

**无 Figma 付费席位时的替代**：
- 用截图 + `knowledge/prompts/page-from-design.md` 模板做图像还原（效果略逊于 MCP，但零成本）。
- 国产工具 MasterGo / 即时设计各有自己的标注/交付方案，接入思路相同，配置时在 `toolchain.json` 登记即可。

## 2. 浏览器自动化（走查必需）

走查卡点（G4）要求像素级对比与交互验证，用本环境已有的浏览器控制能力即可：
- 打开 `output/<项目名>/` 本地页面 → 逐视口（1280/768/375）截图。
- 与视觉稿截图叠加/并排对比，差异记入 `04-走查报告.md`。
- 扫描 Console 报错与网络失败请求。

无现成浏览器能力时，用 Playwright 脚本替代（`toolchain.json` quality 类）。

## 3. Style Directory（tokens 构建管道）[可选]

tokens.json 是单一事实源，代码侧消费方式二选一：
- **轻量方案（默认）**：tokens.json 中 `$value` 直接由 AI 转写为 `tokens.css`（CSS variables），改动时同步两处。适合页面数 <20 的项目。
- **管道方案**：引入 [Style Dictionary](https://amzn.github.io/style-dictionary/)，`npx style-dictionary build` 从 tokens.json 自动生成 CSS/TS 变量。适合多项目共享 tokens。

## 4. 登记义务

无论接入哪个新工具/MCP，都必须：
1. 更新 `toolchain.json`（含 when / ai_access / alternatives）；
2. 若影响流程某阶段，同步更新 `workflow/pipeline.md` 对应阶段的"AI 参与方式"。
