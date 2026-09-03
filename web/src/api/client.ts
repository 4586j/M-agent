import type {
  WorkerInfo, TaskRecord, DispatchResult, Stats, RegisterBody, DispatchBody,
} from '../types'

// 相对路径：dev 走 vite proxy，prod 同源直调
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
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

  dispatch: (body: DispatchBody) =>
    request<DispatchResult>('/dispatch', { method: 'POST', body: JSON.stringify(body) }),

  artifact: (workerId: string, taskId: string, path: string) =>
    request<{ content: string; encoding?: string; size?: number }>(
      `/proxy/artifact?worker_id=${encodeURIComponent(workerId)}&task_id=${encodeURIComponent(taskId)}&path=${encodeURIComponent(path)}`,
    ),
}
