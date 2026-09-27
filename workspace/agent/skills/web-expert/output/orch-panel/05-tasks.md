# 面板一期改造 · 任务拆解（05-tasks.md）

> Spec 来源：`docs/客户端界面设计.md`（一~五节）+ `web/design/prototype-mint-v1.html`（权威视觉规格）。
> 范围：一期「实时与审批」。MoSCoW：M=本期必须，S=应做，C=可缓。

| # | 任务 | 优先级 | 完成定义 |
|---|---|---|---|
| T1 | Mint tokens 进 tailwind.config（色阶+阴影） | M | `npm run build` 通过；业务代码可用 mint-* 类 |
| T2 | `hooks/useTaskEvents.ts`：从 TaskDetail 提炼 SSE 订阅（快照/终态关流/降级轮询/就地刷新） | M | hook 单一职责；行为与原实现一致 |
| T3 | 组件三件：EventTimeline（mint 竖轴）/ UsageCard（tokens/turns/cost/缓存命中）/ ApprovalBar（裁决理由+prompt 修订+放行/拒绝） | M | 各自独立可复用；对接 approve/reject 真实 API |
| T4 | TaskDetailDrawer（右侧 40% 抽屉）迁移 TaskDetail 全部能力 + TaskList 改挂抽屉；删除旧 TaskDetail | M | WorkerCard/NodeManage/Command 引用链不破 |
| T5 | views/Tasks.tsx：筛选 Chip（状态/角色/worker/搜索/仅运行中）+ 列表 + 抽屉 | M | 全量任务可检索、可实时追踪 |
| T6 | App 顶栏：7 视图导航（滑动 glass pill）+ SSE 状态点 + ConnectionSettings（下拉）+ 新派发入口；memory/schedules/audit 占位 | M | 未实现视图有明确占位说明 |
| T7 | Dashboard 改版：mint 统计卡（含缓存命中率聚合）+ 待办中心（审批/失败，行内操作）+ 状态分布条 | M | 审批可在总览 10 秒内完成 |
| T8 | StatusBadge：awaiting_approval violet→amber（对齐 token 语义） | S | 全局状态色唯一语义 |
| T9 | 构建自检（G3）+ 三视口不破版走查 | M | `npm run build` 0 错误 |

偏移日志：无（首版）。

## 实施记录（2026-09-08 · 一期完成）

- T1~T8 全部完成：`web/tailwind.config.js`（mint 色阶+阴影）、`hooks/useTaskEvents.ts`、
  `components/{EventTimeline,UsageCard,ApprovalBar,TaskDetailDrawer}.tsx`、`views/Tasks.tsx`、
  `App.tsx`（7 视图+滑动 glass pill+SSE 状态点+🔑 下拉）、`Dashboard.tsx`（待办中心+
  缓存命中率聚合+状态分布）、`StatBar.tsx`、`StatusBadge.tsx`（awaiting_approval→amber）、
  `ConnectionSettings.tsx`（顶栏下拉化）；旧 `TaskDetail.tsx` 删除，TaskList 改挂抽屉。
- G3：`npm run build` 通过（tsc 0 错误，bundle 220KB）。
- S4 走查：vite dev + brain(local)+6 条种子任务实测——总览（统计卡/待办中心/分布/成本）、
  任务页（筛选 Chip 带计数）、审批抽屉（裁决理由/原指令/修订框/SSE 时间线/用量卡）、
  375px 移动端不破版，全部通过。三视口 1280 已截图验证，768 复用同一响应式栅格。
- 遗留到二期/三期：记忆四 Tab、异步派发命令台、调度、审计（占位页已说明）。
