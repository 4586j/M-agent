import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTaskEvents } from './useTaskEvents'
import type { TaskRecord } from '../types'

// ---- EventSource mock：记录实例、监听器，支持手动 emit / fail ----

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onerror: (() => void) | null = null
  private listeners = new Map<string, ((e: { data: string }) => void)[]>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  addEventListener(type: string, fn: (e: { data: string }) => void) {
    const arr = this.listeners.get(type) ?? []
    arr.push(fn)
    this.listeners.set(type, arr)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data: Record<string, unknown>) {
    for (const fn of this.listeners.get(type) ?? []) fn({ data: JSON.stringify(data) })
  }
  fail() {
    this.onerror?.()
  }
}

// ---- api mock：api.task 返回可编排的队列值 ----

const taskQueue: TaskRecord[] = []
const taskSpy = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    task: (...args: unknown[]) => taskSpy(...args),
  },
  sseUrl: (id: string) => `/tasks/${id}/events`,
}))

function makeTask(overrides: Partial<TaskRecord> = {}): TaskRecord {
  return {
    task_id: 'task-t1', role: 'executor', prompt: 'p', worker_id: 'w1',
    status: 'running', created_at: '100', updated_at: '100',
    output: null, artifacts: [], error: null, ...overrides,
  }
}

const onChange = vi.fn()

beforeEach(() => {
  MockEventSource.instances = []
  taskQueue.length = 0
  taskSpy.mockReset()
  taskSpy.mockImplementation(() => {
    const next = taskQueue.shift()
    return next ? Promise.resolve(next) : Promise.reject(new Error('no queued task'))
  })
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function flush() {
  return act(async () => {
    await Promise.resolve()
  })
}

describe('useTaskEvents', () => {
  it('非终态任务建立 SSE 订阅，channel=sse', () => {
    const { result } = renderHook(() => useTaskEvents(makeTask(), onChange))
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toContain('/tasks/task-t1/events')
    expect(result.current.channel).toBe('sse')
  })

  it('status 事件追加时间线并刷新任务记录', async () => {
    const updated = makeTask({ status: 'running', output: '进行中输出' })
    taskQueue.push(updated)
    const { result } = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.emit('status', { state: 'running', summary: 'worker 执行中' }))
    await flush()
    expect(result.current.timeline).toHaveLength(1)
    expect(result.current.timeline[0]).toMatchObject({ kind: 'status', state: 'running', summary: 'worker 执行中' })
    expect(result.current.current.output).toBe('进行中输出') // 刷新生效
  })

  it('snapshot + 终态 → 主动关流（channel=off）', () => {
    const { result } = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.emit('status', { state: 'done', snapshot: true }))
    expect(es.closed).toBe(true)
    expect(result.current.channel).toBe('off')
    expect(result.current.timeline).toHaveLength(0) // 快照不进时间线
  })

  it('done 事件 → 关流 + 刷新 + 通知 onChange', async () => {
    const done = makeTask({ status: 'done', output: '最终产出' })
    taskQueue.push(done)
    const { result } = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.emit('done', { state: 'done', summary: '完成' }))
    await flush()
    expect(es.closed).toBe(true)
    expect(result.current.channel).toBe('off')
    expect(result.current.current.output).toBe('最终产出')
    expect(onChange).toHaveBeenCalled()
    expect(result.current.timeline).toHaveLength(1)
  })

  it('终态任务不建立订阅', () => {
    renderHook(() => useTaskEvents(makeTask({ status: 'done' }), onChange))
    expect(MockEventSource.instances).toHaveLength(0)
  })

  it('空 task_id 不建立订阅', () => {
    renderHook(() => useTaskEvents(makeTask({ task_id: '' }), onChange))
    expect(MockEventSource.instances).toHaveLength(0)
  })

  it('onerror → 关流并降级 5s 轮询（间隔与回调可手动驱动）', async () => {
    const polled = makeTask({ output: '轮询拿到的输出' })
    const intervalSpy = vi.spyOn(window, 'setInterval')
    const { result } = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.fail())
    expect(es.closed).toBe(true)
    expect(result.current.channel).toBe('poll')
    // 最后注册的 interval 是 5s 轮询
    const pollCalls = intervalSpy.mock.calls.filter(([, ms]) => ms === 5000)
    expect(pollCalls.length).toBeGreaterThan(0)
    const pollFn = pollCalls[0][0] as () => void
    taskQueue.push(polled)
    await act(async () => {
      pollFn()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(taskSpy).toHaveBeenCalled()
    expect(result.current.current.output).toBe('轮询拿到的输出')
  })

  it('卸载时关闭连接（无泄漏）', () => {
    const { unmount } = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    unmount()
    expect(es.closed).toBe(true)
  })
})
