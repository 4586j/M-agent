import type {
  WorkerInfo, TaskRecord, DispatchResult, DispatchAsyncResponse, Stats, RegisterBody, DispatchBody,
  RecommendBody, RecommendResponse, TranslateBody, TranslateResponse,
  MemoryCoreItem, ContextNotesResponse, ExperienceRecord, ExperienceSearchBody,
  ProposalRecord, ReflectionReport, ScheduleRecord, ScheduleBody, AuditEntry,
} from '../types'

// 相对路径：dev 走 vite proxy，prod 同源直调。
// 鉴权：配置了 ORCH_API_KEY 的部署要求所有非 public 请求携带 X-API-Key 头，
// 否则 /stats /tasks approve/reject 等全部 401。取值与 sseUrl() 同源
// （localStorage.orch_api_key）；key 为空时不发头，本机无 key 部署行为不变
export function getApiKey(): string {
  try {
    return localStorage.getItem('orch_api_key') || ''
  } catch {
    return ''
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const key = getApiKey()
  if (key) headers['X-API-Key'] = key
  const r = await fetch(url, {
    ...init,
    headers,
  })
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = await r.json()
      detail = body.detail || body.error || detail
    } catch {
      /* 非 JSON 响应 */
    }
    throw new Error(`${r.status}: ${detail}`)
  }
  return r.json() as Promise<T>
}

export const api = {
  stats: () => request<Stats>('/stats'),
  workers: () => request<WorkerInfo[]>('/workers'),
  tasks: (workerId?: string) =>
    request<TaskRecord[]>(`/tasks${workerId ? `?worker_id=${encodeURIComponent(workerId)}` : ''}`),
  task: (id: string) => request<TaskRecord>(`/tasks/${encodeURIComponent(id)}`),

  register: (body: RegisterBody) =>
    request<{ status: string; worker_id: string }>('/register', {
      method: 'POST', body: JSON.stringify(body),
    }),
  heartbeat: (workerId: string, load = 0) =>
    request<{ status: string }>('/heartbeat', {
      method: 'POST', body: JSON.stringify({ worker_id: workerId, load }),
    }),
  deleteWorker: (id: string) =>
    request<{ status: string }>(`/workers/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  cancelTask: (id: string) =>
    request<TaskRecord>(`/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  redispatchTask: (id: string, model?: string) =>
    request<DispatchResult>(`/tasks/${encodeURIComponent(id)}/redispatch`, {
      method: 'POST', body: JSON.stringify({ model: model || null }),
    }),
  // 审批：修订 prompt 非空时重过 security_guard；不传则按原指令人工放行
  approveTask: (id: string, prompt?: string) =>
    request<DispatchResult>(`/tasks/${encodeURIComponent(id)}/approve`, {
      method: 'POST', body: JSON.stringify({ prompt: prompt || null }),
    }),
  // 拒绝：任务终态 failed，写审计并广播事件
  rejectTask: (id: string, reason?: string) =>
    request<TaskRecord>(`/tasks/${encodeURIComponent(id)}/reject`, {
      method: 'POST', body: JSON.stringify({ reason: reason || null }),
    }),

  dispatch: (body: DispatchBody) =>
    request<DispatchResult>('/dispatch', { method: 'POST', body: JSON.stringify(body) }),

  // 异步派发：立即返回 task_id，状态经 /tasks/{id} 或 SSE 跟踪（长任务/流程型技能）
  dispatchAsync: (body: DispatchBody) =>
    request<DispatchAsyncResponse>('/dispatch/async', { method: 'POST', body: JSON.stringify(body) }),

  // ---- 记忆系统（二期） ----

  memoryCore: () => request<{ memories: MemoryCoreItem[] }>('/memory/core'),
  memoryCorePut: (name: string, content: string) =>
    request<{ status: string; name: string }>(`/memory/core/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ content }),
    }),
  memoryCoreDelete: (name: string) =>
    request<{ status: string }>(`/memory/core/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  taskContext: (taskId: string) =>
    request<ContextNotesResponse>(`/memory/context/${encodeURIComponent(taskId)}`),
  taskContextPut: (taskId: string, key: string, content: string) =>
    request<{ status: string }>(
      `/memory/context/${encodeURIComponent(taskId)}/${encodeURIComponent(key)}`,
      { method: 'PUT', body: JSON.stringify({ content }) },
    ),
  taskContextArchive: (taskId: string) =>
    request<{ status: string }>(`/memory/context/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),

  experiences: () => request<ExperienceRecord[]>('/experiences'),
  searchExperiences: (body: ExperienceSearchBody) =>
    request<ExperienceRecord[]>('/experiences/search', { method: 'POST', body: JSON.stringify(body) }),
  deleteExperience: (id: string) =>
    request<{ status: string }>(`/experiences/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  reindexExperiences: () =>
    request<{ status: string; reindexed: number }>('/experiences/reindex', { method: 'POST' }),

  // ---- 程序记忆演化（提案-共识-应用） ----

  reflection: (window = 50) =>
    request<ReflectionReport>(`/memory/procedural/reflection?window=${window}`),
  proposals: (status?: string) =>
    request<ProposalRecord[]>(
      `/memory/procedural/proposals${status ? `?status=${encodeURIComponent(status)}` : ''}`,
    ),
  propose: (body: {
    target_path: string; content: string; reason: string; evidence_exp_ids?: string[]
  }) => request<ProposalRecord>('/memory/procedural/proposals', {
    method: 'POST', body: JSON.stringify(body),
  }),
  voteProposal: (id: string, voter: string, verdict: 'pass' | 'block', reason = '') =>
    request<ProposalRecord>(`/memory/procedural/proposals/${encodeURIComponent(id)}/vote`, {
      method: 'POST', body: JSON.stringify({ voter, verdict, reason }),
    }),
  deleteProposal: (id: string) =>
    request<{ status: string }>(`/memory/procedural/proposals/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  // ---- 调度与审计（三期） ----

  schedules: () => request<ScheduleRecord[]>('/schedules'),
  createSchedule: (body: ScheduleBody) =>
    request<ScheduleRecord>('/schedules', { method: 'POST', body: JSON.stringify(body) }),
  deleteSchedule: (id: string) =>
    request<{ status: string }>(`/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  toggleSchedule: (id: string) =>
    request<ScheduleRecord>(`/schedules/${encodeURIComponent(id)}/toggle`, { method: 'POST' }),
  triggerSchedule: (id: string) =>
    request<{ status: string }>(`/schedules/${encodeURIComponent(id)}/trigger`, { method: 'POST' }),

  audit: (params?: { stage?: string; decision?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.stage) q.set('stage', params.stage)
    if (params?.decision) q.set('decision', params.decision)
    if (params?.limit) q.set('limit', String(params.limit))
    const qs = q.toString()
    return request<AuditEntry[]>(`/audit${qs ? `?${qs}` : ''}`)
  },

  // AI 推荐/解释顾问（独立上下文，不污染任务上下文）
  recommend: (body: RecommendBody) =>
    request<RecommendResponse>('/recommend', { method: 'POST', body: JSON.stringify(body) }),

  // 结果翻译（独立上下文，思考英文/结果中文）
  translate: (body: TranslateBody) =>
    request<TranslateResponse>('/translate', { method: 'POST', body: JSON.stringify(body) }),

  artifact: (workerId: string, taskId: string, path: string) =>
    request<{ content: string; encoding?: string; size?: number }>(
      `/proxy/artifact?worker_id=${encodeURIComponent(workerId)}&task_id=${encodeURIComponent(taskId)}&path=${encodeURIComponent(path)}`,
    ),
}

// SSE 实时事件流地址。EventSource 无法携带自定义 header，
// 部署了 api_key 时从 localStorage.orch_api_key 读取并走 ?api_key= 查询参数
// （brain 的 auth 中间件同时接受 X-API-Key 头与查询参数；未设 key 的部署无需配置）
export function sseUrl(taskId: string): string {
  const key = getApiKey()
  const q = key ? `?api_key=${encodeURIComponent(key)}` : ''
  return `/tasks/${encodeURIComponent(taskId)}/events${q}`
}
