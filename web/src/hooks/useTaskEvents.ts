import { useEffect, useRef, useState } from 'react'
import type { TaskRecord, TaskStatus } from '../types'
import { TERMINAL_STATUSES } from '../types'
import { POLL } from '../polling'
import { api, sseUrl } from '../api/client'
import { getActiveConnectionScope, useActiveConnectionScope } from '../api/connections'

/** SSE 事件时间线行 */
export interface TaskEventLine {
  at: number
  kind: 'status' | 'callback' | 'done'
  state?: string
  summary?: string
  snapshot?: boolean
}

export type EventChannel = 'sse' | 'poll' | 'off'

/**
 * 单任务实时订阅：SSE 事件时间线 + 就地刷新；EventSource 失败时降级为 5s 轮询。
 * 从原 TaskDetail 内联逻辑提炼，行为保持一致（快照/终态关流/done 后 refresh）。
 */
export function useTaskEvents(
  task: TaskRecord,
  onChange?: () => void,
): { current: TaskRecord; timeline: TaskEventLine[]; channel: EventChannel } {
  const [live, setLive] = useState<TaskRecord | null>(null)
  const [timeline, setTimeline] = useState<TaskEventLine[]>([])
  const [channel, setChannel] = useState<EventChannel>('off')
  const onChangeRef = useRef(onChange)
  const scope = useActiveConnectionScope()
  const identity = JSON.stringify([scope, task.task_id])
  const stateIdentity = useRef(identity)
  onChangeRef.current = onChange

  useEffect(() => {
    stateIdentity.current = identity
    setLive(null)
    setTimeline([])
    setChannel('off')
    if (!task.task_id || TERMINAL_STATUSES.includes(task.status)) return // 终态/空任务无事件可等

    let disposed = false
    let streamEnded = false
    let notified = false
    let request = 0
    let settled = 0
    let responseFloor = 0
    let poll: number | null = null
    const controller = new AbortController()
    const valid = () => !disposed && getActiveConnectionScope() === scope
    const es = new EventSource(sseUrl(task.task_id))
    const finish = () => {
      streamEnded = true
      es.close()
      if (poll !== null) { clearInterval(poll); poll = null }
      setChannel('off')
    }
    const notify = () => {
      if (!notified) { notified = true; onChangeRef.current?.() }
    }
    const refresh = async (terminal = false) => {
      if (!valid()) return
      const sequence = ++request
      try {
        const next = await api.task(task.task_id, controller.signal)
        if (!valid() || sequence < settled || sequence < responseFloor) return
        settled = sequence
        setLive(next)
        if (terminal || TERMINAL_STATUSES.includes(next.status)) {
          finish()
          notify()
        }
      } catch {
        /* 静默失败，保留现有展示 */
      }
    }

    setChannel('sse')
    const handle = (kind: TaskEventLine['kind']) => (e: MessageEvent) => {
      if (!valid() || streamEnded) return
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(e.data)
      } catch {
        return // 忽略坏帧，不触发详情请求
      }
      // 终态快照关闭事件流，并拉取最终输出。
      // （data.state 来自 JSON 边界，cast 到 TaskStatus 后做终态判定）
      if (data.snapshot === true && typeof data.state === 'string'
        && TERMINAL_STATUSES.includes(data.state as TaskStatus)) {
        responseFloor = request + 1
        finish()
        const status = data.state as TaskStatus
        setLive(prev => ({ ...(prev ?? task), status }))
        void refresh(true)
        return
      }
      setTimeline((prev) => [
        ...prev.slice(-99),
        {
          at: typeof data.timestamp === 'number' ? data.timestamp : Date.now() / 1000,
          kind,
          state: typeof data.state === 'string' ? data.state : undefined,
          summary: typeof data.summary === 'string' ? data.summary : undefined,
          snapshot: data.snapshot === true,
        },
      ])
      if (kind === 'done') {
        responseFloor = request + 1
        finish()
        void refresh(true)
      } else if (data.snapshot !== true) {
        void refresh() // 状态/回调实质变化 → 拉最新记录（output/meta）
      }
    }
    es.addEventListener('status', handle('status'))
    es.addEventListener('callback', handle('callback'))
    es.addEventListener('done', handle('done'))
    es.onerror = () => {
      if (!valid() || streamEnded) return
      // SSE 不可用（代理/网络限制）→ 降级为 5s 轮询
      es.close()
      streamEnded = true
      setChannel('poll')
      poll = window.setInterval(() => { void refresh() }, POLL.tasks)
    }
    return () => {
      disposed = true
      controller.abort()
      es.close()
      if (poll !== null) clearInterval(poll)
    }
    // 任务或连接变化时重建订阅（task.status 取打开时刻快照）。
    // scope：连接、地址或密钥变化后必须关闭旧订阅并丢弃迟到响应。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.task_id, scope])

  const current = stateIdentity.current === identity
  return { current: current ? live ?? task : task,
    timeline: current ? timeline : [], channel: current ? channel : 'off' }
}
