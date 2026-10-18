import { useSyncExternalStore } from 'react'

/**
 * 多 brain 连接：面板可保存多个大脑连接（名称 + 地址 + API Key）并切换。
 *
 * 存储：localStorage.orch_connections（数组）+ orch_active_connection（活动 id）。
 * 迁移：首启若没有连接，从遗留的 orch_api_key 播种一条默认连接（baseUrl=''，
 * 空串 = 相对路径 = dev 走 vite proxy / prod 同源直调，行为与旧版一致）。
 * 活动连接的 apiKey 镜像写回遗留键 orch_api_key：getApiKey() 继续读该键，
 * 外部任何读它的地方（含 sseUrl 的 ?api_key= 参数）自动保持一致。
 */
export interface Connection {
  id: string
  name: string
  baseUrl: string
  apiKey: string
}

interface ConnectionsState {
  connections: Connection[]
  activeId: string
}

const LS_CONNS = 'orch_connections'
const LS_ACTIVE = 'orch_active_connection'

const listeners = new Set<() => void>()
let state: ConnectionsState = loadInitial()
// Runtime revisions invalidate requests/forms after endpoint or credential edits.
// Credentials themselves must never appear in React keys, URLs or diagnostics.
const revisions = new Map<string, number>()

function normalizeBaseUrl(raw: string): string {
  const t = (raw || '').trim()
  if (!t) return ''
  return t.replace(/\/+$/, '')
}

function loadInitial(): ConnectionsState {
  try {
    const raw = localStorage.getItem(LS_CONNS)
    if (raw) {
      const conns = JSON.parse(raw) as Connection[]
      if (Array.isArray(conns) && conns.length > 0) {
        const activeId = localStorage.getItem(LS_ACTIVE)
        const active = conns.some((c) => c.id === activeId) ? (activeId as string) : conns[0].id
        return { connections: conns, activeId: active }
      }
    }
  } catch {
    /* 损坏数据按首启处理 */
  }
  const legacyKey = (() => {
    try {
      return localStorage.getItem('orch_api_key') || ''
    } catch {
      return ''
    }
  })()
  return {
    connections: [{ id: 'default', name: '默认', baseUrl: '', apiKey: legacyKey }],
    activeId: 'default',
  }
}

function commit(next: ConnectionsState): void {
  state = next
  try {
    localStorage.setItem(LS_CONNS, JSON.stringify(next.connections))
    localStorage.setItem(LS_ACTIVE, next.activeId)
    const active = next.connections.find((c) => c.id === next.activeId)
    // 镜像写回遗留键：getApiKey() 与 sseUrl() 读同一个键，保持外部读取一致
    if (active?.apiKey) localStorage.setItem('orch_api_key', active.apiKey)
    else localStorage.removeItem('orch_api_key')
  } catch {
    /* localStorage 不可用（隐私模式等）降级为内存态 */
  }
  listeners.forEach((l) => l())
}

function nextId(): string {
  return `conn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export function getConnections(): Connection[] {
  return state.connections
}

export function getActiveConnection(): Connection {
  const c = state.connections.find((x) => x.id === state.activeId)
  return c ?? state.connections[0] ?? { id: 'default', name: '默认', baseUrl: '', apiKey: '' }
}

export function getActiveConnectionId(): string {
  return getActiveConnection().id
}

export function getActiveConnectionScope(): string {
  const c = getActiveConnection()
  return JSON.stringify([c.id, normalizeBaseUrl(c.baseUrl), revisions.get(c.id) ?? 0])
}

export function useActiveConnectionScope(): string {
  return useSyncExternalStore(subscribe, getActiveConnectionScope)
}

export function createConnection(
  name: string,
  baseUrl: string,
  apiKey: string,
): Connection {
  return { id: nextId(), name: name.trim(), baseUrl: normalizeBaseUrl(baseUrl), apiKey }
}

export function saveConnection(conn: Connection): void {
  // baseUrl 规范化下沉到保存入口：表单编辑产生的 draft 可能带尾部斜杠/空白
  const norm = { ...conn, baseUrl: normalizeBaseUrl(conn.baseUrl) }
  const existing = state.connections.find((c) => c.id === norm.id)
  if (existing && (existing.baseUrl !== norm.baseUrl || existing.apiKey !== norm.apiKey)) {
    revisions.set(norm.id, (revisions.get(norm.id) ?? 0) + 1)
  }
  const conns = existing
    ? state.connections.map((c) => (c.id === norm.id ? norm : c))
    : [...state.connections, norm]
  commit({ connections: conns, activeId: state.activeId })
}

export function activate(id: string): void {
  if (!state.connections.some((c) => c.id === id)) return
  commit({ connections: state.connections, activeId: id })
}

export function deleteConnection(id: string): void {
  const conns = state.connections.filter((c) => c.id !== id)
  if (conns.length === 0) return
  let activeId = state.activeId
  if (activeId === id) activeId = conns[0].id
  commit({ connections: conns, activeId })
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function getSnapshot(): ConnectionsState {
  return state
}

/** React hook：当前活动连接 id。切换连接时返回新引用，驱动 usePolling/SSE 重取。 */
export function useActiveConnectionId(): string {
  return useSyncExternalStore(subscribe, getSnapshot).activeId
}

export function useActiveConnection(): Connection {
  const id = useActiveConnectionId()
  return state.connections.find((c) => c.id === id) ?? state.connections[0]
}

/**
 * 测试连接可达性与鉴权：裸 fetch（不走 api.request——后者吞掉状态码），
 * 区分 200（正常）/ 401（可达但 Key 错）/ 403（非回环 brain 未配 Key 被拒）/
 * 网络错误（不可达）。
 */
export async function testConnection(conn: Connection): Promise<{
  ok: boolean
  status: number | null
  message: string
}> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 5000)
  try {
    const headers: Record<string, string> = {}
    if (conn.apiKey) headers['X-API-Key'] = conn.apiKey
    const r = await fetch(`${conn.baseUrl}/stats`, {
      headers,
      signal: controller.signal,
    })
    if (r.ok) return { ok: true, status: r.status, message: '连接正常' }
    if (r.status === 401) return { ok: false, status: 401, message: '可达，但 API Key 鉴权失败' }
    if (r.status === 403) return { ok: false, status: 403, message: '可达，但被拒绝（非回环 brain 未配 Key？）' }
    return { ok: false, status: r.status, message: `HTTP ${r.status}` }
  } catch {
    return { ok: false, status: null, message: '网络不可达' }
  } finally {
    clearTimeout(timer)
  }
}
