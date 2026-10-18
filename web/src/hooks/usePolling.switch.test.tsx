import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePolling } from './usePolling'
import {
  createConnection,
  saveConnection,
  activate,
  deleteConnection,
  getActiveConnectionId,
  getConnections,
  getActiveConnection,
} from '../api/connections'

/**
 * usePolling 多 brain 连接切换：connId 进 dep 自动重拉 + epoch 守卫丢弃迟到响应。
 * 用可手动 resolve 的 deferred 精确控制旧/新连接请求的落地顺序。
 * 连接单例状态跨测试共享，故测试前清到只剩默认连接。
 */
beforeEach(() => {
  for (const c of getConnections()) {
    if (c.id !== 'default') deleteConnection(c.id)
  }
})

function deferred() {
  const resolvers: Array<(v: string) => void> = []
  const fn = vi.fn(() => new Promise<string>((res) => resolvers.push(res)))
  return { resolvers, fn }
}

describe('usePolling · 多 brain 连接切换', () => {
  it.each(['baseUrl', 'apiKey'] as const)('修改同一连接的 %s 立即重拉并丢弃旧响应', async field => {
    const {resolvers, fn} = deferred()
    const {result} = renderHook(() => usePolling(fn, 100000))
    expect(resolvers).toHaveLength(1)
    act(() => saveConnection({...getActiveConnection(), [field]: field === 'baseUrl' ? 'http://changed.test' : 'rotated-key'}))
    expect(resolvers).toHaveLength(2)
    expect(result.current.data).toBeNull()
    await act(async () => {resolvers[1]('new'); await Promise.resolve()})
    await act(async () => {resolvers[0]('old'); await Promise.resolve()})
    expect(result.current.data).toBe('new')
  })

  it('仅修改连接名称保留现有数据且不重拉', async () => {
    const fn = vi.fn(async () => 'kept')
    const {result} = renderHook(() => usePolling(fn, 100000))
    await waitFor(() => expect(result.current.data).toBe('kept'))
    act(() => saveConnection({...getActiveConnection(), name:'重命名'}))
    expect(fn).toHaveBeenCalledTimes(1)
    expect(result.current.data).toBe('kept')
  })

  it('并发刷新时较早请求迟到不覆盖最新结果', async () => {
    const {resolvers, fn} = deferred()
    const {result} = renderHook(() => usePolling(fn, 100000))
    let newer!: Promise<void>
    act(() => {newer = result.current.refresh()})
    await act(async () => {resolvers[1]('new'); await newer})
    await act(async () => {resolvers[0]('old'); await Promise.resolve()})
    expect(result.current.data).toBe('new')
  })

  it('慢请求期间开启下一次刷新，不会使所有较早响应都失效', async () => {
    const {resolvers, fn} = deferred()
    const {result} = renderHook(() => usePolling(fn, 100000))
    let newer!: Promise<void>
    act(() => {newer = result.current.refresh()})
    await act(async () => {resolvers[0]('first'); await Promise.resolve()})
    expect(result.current.data).toBe('first')
    await act(async () => {resolvers[1]('newer'); await newer})
    expect(result.current.data).toBe('newer')
  })

  it('切换连接后旧数据清空、新数据到达后显示', async () => {
    const other = createConnection('第二大脑', 'http://127.0.0.1:7001', '')
    saveConnection(other)
    const { resolvers, fn } = deferred()

    const { result } = renderHook(() => usePolling<string>(fn, 100000))
    await waitFor(() => expect(resolvers).toHaveLength(1)) // mount 首次 tick
    await act(async () => {
      resolvers[0]('default')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current?.data).toBe('default'))

    act(() => activate(other.id))
    await waitFor(() => expect(resolvers).toHaveLength(2)) // 切换即重建定时器重拉
    await act(async () => { await Promise.resolve() })
    // 新请求未落地前：数据已清空（不闪旧 brain 内容）
    expect(result.current?.data).toBeNull()
    await act(async () => {
      resolvers[1](other.id)
      await Promise.resolve()
    })
    expect(result.current?.data).toBe(other.id)
  })

  it('迟到的旧连接响应被 epoch 守卫丢弃，不覆盖新数据', async () => {
    const other = createConnection('慢脑', 'http://127.0.0.1:7002', '')
    saveConnection(other)
    const { resolvers, fn } = deferred()

    const { result } = renderHook(() => usePolling<string>(fn, 100000))
    await waitFor(() => expect(resolvers).toHaveLength(1)) // 旧连接请求在飞

    act(() => activate(other.id))
    await waitFor(() => expect(resolvers).toHaveLength(2)) // 新连接请求在飞

    // 旧连接响应迟到落地：epoch 不匹配 → 丢弃，data 不动
    await act(async () => {
      resolvers[0]('OLD-BRAIN')
      await Promise.resolve()
    })
    expect(result.current?.data).toBeNull()

    // 新连接响应落地：才真正更新
    await act(async () => {
      resolvers[1]('NEW-BRAIN')
      await Promise.resolve()
    })
    expect(result.current?.data).toBe('NEW-BRAIN')
  })

  it('活动连接被删时回退并跟随重拉', async () => {
    const other = createConnection('第二大脑', 'http://127.0.0.1:7001', '')
    saveConnection(other)
    const { resolvers, fn } = deferred()

    const { result } = renderHook(() => usePolling<string>(fn, 100000))
    await waitFor(() => expect(resolvers).toHaveLength(1))
    await act(async () => {
      resolvers[0](getActiveConnectionId())
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current?.data).toBe('default'))

    act(() => activate(other.id))
    await waitFor(() => expect(resolvers).toHaveLength(2))
    await act(async () => {
      resolvers[1](other.id)
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current?.data).toBe(other.id))

    act(() => deleteConnection(other.id))
    await waitFor(() => expect(resolvers).toHaveLength(3)) // 回退后重拉
    await act(async () => {
      resolvers[2]('default')
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current?.data).toBe('default'))
  })
})
