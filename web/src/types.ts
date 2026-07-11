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

export type TaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'lost' | 'cancelled'

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
  verdict?: SecurityVerdict  // status=='blocked' 时存在（指令拦截）
  product_verdict?: SecurityVerdict  // status=='done' 时可能存在（产物拦截）
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
}
