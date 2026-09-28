import { describe, it, expect, beforeEach, vi } from 'vitest'

/**
 * 连接管理器纯逻辑测试：每次重导入模块（模块级 state 在 import 时捕获，
 * 避免测试间串状态）。localStorage 由 jsdom 提供。
 */
async function loadApi() {
  vi.resetModules()
  return await import('./connections')
}

beforeEach(() => {
  localStorage.clear()
})

describe('connections', () => {
  it('首启从遗留 orch_api_key 播种默认连接（baseUrl 为空 = 相对路径）', async () => {
    localStorage.setItem('orch_api_key', 'legacy-key')
    const { getActiveConnection, getConnections } = await loadApi()
    expect(getConnections()).toHaveLength(1)
    const c = getActiveConnection()
    expect(c.name).toBe('默认')
    expect(c.baseUrl).toBe('')
    expect(c.apiKey).toBe('legacy-key')
  })

  it('已有连接时不再迁移，活动连接读 orch_active_connection', async () => {
    localStorage.setItem('orch_connections', JSON.stringify([
      { id: 'a', name: 'A', baseUrl: 'http://127.0.0.1:7001', apiKey: 'ka' },
      { id: 'b', name: 'B', baseUrl: 'http://127.0.0.1:7002', apiKey: 'kb' },
    ]))
    localStorage.setItem('orch_active_connection', 'b')
    const { getActiveConnection } = await loadApi()
    expect(getActiveConnection().id).toBe('b')
    expect(getActiveConnection().baseUrl).toBe('http://127.0.0.1:7002')
  })

  it('活动连接 id 被删时回退到剩余第一个', async () => {
    localStorage.setItem('orch_connections', JSON.stringify([
      { id: 'a', name: 'A', baseUrl: '', apiKey: '' },
    ]))
    localStorage.setItem('orch_active_connection', 'a')
    const api = await loadApi()
    // 删掉唯一连接不允许（保底留一个）
    api.deleteConnection('a')
    expect(api.getConnections()).toHaveLength(1)
  })

  it('create → activate → save → delete 全流程 + subscribe 通知', async () => {
    const api = await loadApi()
    const seen: string[] = []
    const unsub = api.subscribe(() => seen.push(api.getActiveConnectionId()))

    const conn = api.createConnection(' 第二大脑 ', '  http://127.0.0.1:7001/  ', 'key-2')
    // baseUrl 规范化：去空白、去尾部斜杠
    expect(conn.baseUrl).toBe('http://127.0.0.1:7001')
    expect(conn.name).toBe('第二大脑')

    api.saveConnection(conn)
    api.activate(conn.id)
    expect(api.getActiveConnection().id).toBe(conn.id)
    expect(api.getActiveConnection().apiKey).toBe('key-2')
    // 活动连接 apiKey 镜像写回遗留键：getApiKey() 读同一个键保持一致
    expect(localStorage.getItem('orch_api_key')).toBe('key-2')

    api.activate('default')
    api.deleteConnection(conn.id)
    expect(api.getConnections().map((c) => c.id)).not.toContain(conn.id)
    expect(seen.length).toBeGreaterThan(0)
    unsub()
  })

  it('activate 未知 id 为 no-op，不破坏当前活动连接', async () => {
    const api = await loadApi()
    api.activate('nonexistent')
    expect(api.getActiveConnectionId()).toBe('default')
  })
})

describe('testConnection', () => {
  it('200 → 连接正常', async () => {
    const api = await loadApi()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    const r = await api.testConnection({ id: 'x', name: 'X', baseUrl: 'http://b:7000', apiKey: '' })
    // eslint-disable-next-line no-undef
    expect(r).toEqual({ ok: true, status: 200, message: '连接正常' })
    const fetcher = fetch as unknown as ReturnType<typeof vi.fn>
    const url = String(fetcher.mock.calls[0][0])
    expect(url).toBe('http://b:7000/stats')
    vi.unstubAllGlobals()
  })

  it('401 → 鉴权失败（与 403 区分）', async () => {
    const api = await loadApi()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }))
    const r = await api.testConnection({ id: 'x', name: 'X', baseUrl: 'http://b:7000', apiKey: 'bad' })
    expect(r.status).toBe(401)
    expect(r.ok).toBe(false)
    vi.unstubAllGlobals()
  })

  it('403 → 被拒（loopback-only）', async () => {
    const api = await loadApi()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403 }))
    const r = await api.testConnection({ id: 'x', name: 'X', baseUrl: 'http://b:7000', apiKey: '' })
    expect(r.status).toBe(403)
    vi.unstubAllGlobals()
  })

  it('网络错误 → 不可达', async () => {
    const api = await loadApi()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const r = await api.testConnection({ id: 'x', name: 'X', baseUrl: 'http://down:9', apiKey: '' })
    expect(r.ok).toBe(false)
    expect(r.status).toBeNull()
    expect(r.message).toBe('网络不可达')
    vi.unstubAllGlobals()
  })
})