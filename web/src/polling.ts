/** 轮询间隔（ms）集中常量表：各数据源的新鲜度口径一目了然，改一处全局生效。
 *
 * 语义而非值命名（不同资源可能同间隔）：避免散落魔法数、按资源对齐调整。
 */
export const POLL = {
  tasks: 5000,        // 任务/worker 明细：操作反馈
  tasksSlow: 10000,   // 任务列表/记忆上下文页：次要低频
  workers: 5000,
  stats: 5000,        // /stats（总览页激活时）
  statsSlow: 10000,   // /stats（离开总览页降频）
  services: 8000,     // 记忆/调度/Provider 列表：分钟级变更
  providers: 15000,   // 命令台 Provider 下拉：低变更
  reflection: 15000,  // 反思报告：低变更
} as const