import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTaskEvents } from './useTaskEvents'
import type { TaskRecord } from '../types'
import { getActiveConnection, saveConnection } from '../api/connections'

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
  onChange.mockClear()
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
  it('切换任务后丢弃旧详情响应并取消旧请求', async () => {
    let complete!: (task: TaskRecord) => void
    taskSpy.mockImplementationOnce(() => new Promise(resolve => {complete = resolve}))
    const {result, rerender} = renderHook(({task}) => useTaskEvents(task, onChange),
      {initialProps:{task:makeTask()}})
    act(() => MockEventSource.instances[0].emit('status', {state:'running'}))
    const signal = taskSpy.mock.calls[0][1] as AbortSignal
    rerender({task:makeTask({task_id:'task-t2'})})
    expect(signal.aborted).toBe(true)
    await act(async () => {complete(makeTask({output:'旧任务'})); await Promise.resolve()})
    expect(result.current.current.task_id).toBe('task-t2')
    expect(result.current.current.output).toBeNull()
    expect(result.current.timeline).toEqual([])
    expect(onChange).not.toHaveBeenCalled()
  })

  it.each(['baseUrl', 'apiKey'] as const)('同一连接修改 %s 后重订阅并丢弃旧详情', async field => {
    let complete!: (task: TaskRecord) => void
    taskSpy.mockImplementationOnce(() => new Promise(resolve => {complete = resolve}))
    const {result} = renderHook(() => useTaskEvents(makeTask(), onChange))
    const old = MockEventSource.instances[0]
    act(() => old.emit('status', {state:'running'}))
    act(() => saveConnection({...getActiveConnection(), [field]: field === 'baseUrl' ? 'http://changed.test' : 'rotated-key'}))
    expect(old.closed).toBe(true)
    expect(MockEventSource.instances).toHaveLength(2)
    await act(async () => {complete(makeTask({output:'旧连接'})); await Promise.resolve()})
    expect(result.current.current.output).toBeNull()
    act(() => old.fail())
    expect(result.current.channel).toBe('sse')
  })

  it('终态快照刷新最终输出并通知列表', async () => {
    taskQueue.push(makeTask({status:'done',output:'快照后的最终输出'}))
    const {result} = renderHook(() => useTaskEvents(makeTask(), onChange))
    act(() => MockEventSource.instances[0].emit('status', {state:'done',snapshot:true}))
    expect(result.current.current.status).toBe('done')
    await flush()
    expect(result.current.current.output).toBe('快照后的最终输出')
    expect(result.current.channel).toBe('off')
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('同一任务较早的详情响应不覆盖最新输出', async () => {
    const responses: ((task: TaskRecord) => void)[] = []
    taskSpy.mockImplementation(() => new Promise(resolve => responses.push(resolve)))
    const {result} = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.emit('status', {state:'running'}))
    act(() => es.emit('callback', {state:'running'}))
    await act(async () => {responses[1](makeTask({output:'新'})); await Promise.resolve()})
    await act(async () => {responses[0](makeTask({output:'旧'})); await Promise.resolve()})
    expect(result.current.current.output).toBe('新')
  })

  it('降级轮询到终态后清除定时器，不重复通知', async () => {
    const intervals = vi.spyOn(window, 'setInterval')
    const clear = vi.spyOn(window, 'clearInterval')
    const {result} = renderHook(() => useTaskEvents(makeTask(), onChange))
    const es = MockEventSource.instances[0]
    act(() => es.fail())
    const pollFn = intervals.mock.calls[0][0] as () => void
    taskQueue.push(makeTask({status:'done',output:'完成'}))
    await act(async () => {pollFn(); await Promise.resolve()})
    expect(result.current.channel).toBe('off')
    expect(clear).toHaveBeenCalled()
    expect(onChange).toHaveBeenCalledTimes(1)
    act(() => es.fail())
    expect(intervals).toHaveBeenCalledTimes(1)
  })

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
