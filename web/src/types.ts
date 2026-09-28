// 后端返回的数据结构（与 brain_api / worker_api 对齐）

export interface WorkerInfo {
  worker_id: string
  role: string
  host: string
  port: number
  capabilities: Record<string, unknown>
  last_heartbeat: number // epoch 秒
  load: number
}

// awaiting_approval：安全拦截后等待人工审批（非终态，不参与重试/回收）
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'lost' | 'cancelled' | 'awaiting_approval'

/** worker 回传的 token 明细（口径与 eval/run_eval 一致） */
export interface TaskUsage {
  input_tokens?: number
  output_tokens?: number
  cache_read_input_tokens?: number
  cache_creation_input_tokens?: number
}

/** worker callback 带回的执行元数据（brain 终态化时入库） */
export interface TaskMeta {
  model?: string
  num_turns?: number
  cost_usd?: number
  usage?: TaskUsage
  [key: string]: unknown
}

export interface TaskRecord {
  task_id: string
  role: string
  prompt: string
  worker_id: string | null
  status: TaskStatus
  created_at: string
  updated_at: string
  output: unknown
  artifacts: string[]
  error: unknown
  verdict?: SecurityVerdict
  trace_id?: string | null  // 链路追踪 ID（brain 派发时生成，旧任务为 null）
  meta?: TaskMeta  // 执行元数据（worker 回传 / 审批详情）
}

/** meta.approval：安全拦截后挂起任务的审批详情 */
export interface ApprovalMeta {
  status?: 'pending' | 'approved' | 'rejected'
  verdict?: SecurityVerdict
  original_prompt?: string
  requested_by?: string | null
  rejected_by?: string | null
  reject_reason?: string
}

export interface SecurityVerdict {
  verdict: 'pass' | 'block'
  reason: string
  matched_rules: string[]
  suggestion: string
}

export interface DispatchResult {
  task_id: string
  status: string
  output: unknown
  artifacts: string[]
  error: unknown
  verdict?: SecurityVerdict  // status=='blocked'/'awaiting_approval' 时存在（指令拦截）；approve 修订重过检失败时也可能带回
  product_verdict?: SecurityVerdict  // status=='done' 时可能存在（产物拦截）
  dispatched_task_id?: string  // approve 放行后新派发的任务 ID
  // approve 端点返回：仅 approved===true 视为放行成功；失败体可能无该字段，
  // 形如 {status: "failed", error: "..."}，须同时按 status/error 判定
  approved?: boolean
}

/** 成本聚合桶（tokens/cost 无数据时为 null） */
export interface CostBucket {
  tasks: number
  tokens: number | null
  cost_usd: number | null
}

/** /stats.cost：按日近 7 天 / 按 role / 按 model 聚合 */
export interface CostStats {
  total: CostBucket
  by_day: (CostBucket & { date: string })[]
  by_role: Record<string, CostBucket>
  by_model: Record<string, CostBucket>
}

export interface Stats {
  mode: string
  brain_url: string
  heartbeat_timeout: number
  worker_total: number
  worker_active: number
  task_total: number
  task_status: Record<string, number>
  recent_tasks: TaskRecord[]
  cost?: CostStats  // 成本聚合（无 meta 数据时字段仍存在，数值为 null）
}

export interface RegisterBody {
  worker_id: string
  role: string
  host: string
  port: number
  capabilities: Record<string, unknown>
}

export interface DispatchBody {
  prompt: string
  role: string
  context_memory: string | null
  model: string | null
  force?: boolean
  // 定点派发：指定目标节点 id（null/省略 = 按 role+strategy 自动选节点）
  worker_id?: string | null
  // 子代理派发每次请求覆盖：null/省略=跟随全局；true/false=本次强制启用/禁用
  enable_subagents?: boolean | null
  // 引用历史任务的上下文记忆笔记并入本次派发（续做任务）
  context_task_ids?: string[]
}

/** 顾问拆解计划的单个步骤 */
export interface AdvisorPlanStep {
  id: number | string | null
  action: string
  depends_on: (number | string)[]
}

/** AI 推荐/解释顾问的结构化建议（运行于独立上下文，不污染任务上下文） */
export interface AdvisorResult {
  should_dispatch: boolean
  roles: string[]
  plan: AdvisorPlanStep[]
  explanation: string
  risks: string[]
  mode?: string
}

/** auto-apply 时顾问建议被采纳后派发的任务条目 */
export interface AdvisorDispatched {
  role: string
  task_id?: string
  status?: string
  output?: unknown
  artifacts?: string[]
  error?: unknown
  reason?: string
}

export interface RecommendBody {
  task: string
  mode?: 'pre_flight' | 'plan' | 'explain' | 'all'
  finished_output?: string | null
  enable_subagents?: boolean | null
  auto_apply?: boolean | null
}

export interface RecommendResponse {
  status: string
  error?: string
  recommendation?: AdvisorResult
  auto_applied?: boolean
  dispatched?: AdvisorDispatched[]
}

/** 结果翻译（运行于独立上下文，不污染任务上下文；思考英文/结果中文） */
export interface TranslationResult {
  translated: string
  target_lang: string
  changed: boolean
  skipped?: string  // "empty" | "already_target_lang"
  error?: string
}

export interface TranslateBody {
  text: string
  target_lang?: string | null
}

export interface TranslateResponse {
  status: string
  error?: string
  translation?: TranslationResult
}

// ---- 记忆系统（二期：/memory/* /experiences /dispatch async） ----

/** 核心记忆条目（GET /memory/core） */
export interface MemoryCoreItem {
  name: string
  content: string | null
}

/** 任务上下文笔记（GET /memory/context/{task_id}） */
export interface ContextNotesResponse {
  task_id: string
  notes: Record<string, string | null>
}

/** 历史任务经验记录（/experiences；embedding 字段过大，前端忽略不展示） */
export interface ExperienceRecord {
  exp_id: string
  role: string
  prompt: string
  output_summary: string
  success: boolean
  verdict?: SecurityVerdict | null
  artifacts: string[]
  created_at: string
  tags: string[]
  hits?: number
  last_hit_at?: string
  version?: number
}

export interface ExperienceSearchBody {
  query: string
  role?: string | null
  successful_only?: boolean
  limit?: number
}

/** 程序记忆演化：单视角投票 */
export interface ProceduralVote {
  verdict: 'pass' | 'block'
  reason: string
  at: string
}

/** 演化提案（原内容随提案归档，支撑 diff 对比） */
export interface ProposalRecord {
  proposal_id: string
  target_path: string
  original_content: string
  original_hash: string
  content: string
  reason: string
  evidence_exp_ids: string[]
  votes: Record<string, ProceduralVote>
  status: 'pending' | 'applied' | 'rejected'
  reject_reason: string
  created_at: string
  applied_at: string
}

/** 反思报告（近期经验确定性聚合，提案的证据面板） */
export interface ReflectionReport {
  window: number
  total: number
  success: number
  by_role: Record<string, { total: number; success: number }>
  failed_recent: { exp_id: string; prompt: string }[]
  most_hit: { exp_id: string; hits: number; prompt: string }[]
}

/** 异步派发响应（POST /dispatch/async） */
export interface DispatchAsyncResponse {
  status: string
  task_id: string
  model?: string | null
  error?: string | null
}

// ---- 调度与审计（三期） ----

/** 定时调度任务（/schedules） */
export interface ScheduleRecord {
  schedule_id: string
  name: string
  cron_expr: string
  role: string
  prompt: string
  context_memory: string | null
  model: string | null
  enabled: boolean
  last_run: string
  next_run: string
  created_at: string
}

export interface ScheduleBody {
  name: string
  cron_expr: string
  role: string
  prompt: string
  context_memory?: string | null
  model?: string | null
}

/** 安全裁决审计事件（GET /audit，JSONL 一行一事件） */
export interface AuditEntry {
  timestamp: string
  task_id: string | null
  stage: 'dispatch' | 'product_check' | 'approve'
  decision: 'block' | 'warn' | 'pass' | 'force' | 'approve' | 'reject'
  matched_rules: string[]
  operator: string | null
  llm_enhanced: boolean
  reason?: string
}
