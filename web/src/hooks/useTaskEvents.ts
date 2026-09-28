import { useEffect, useRef, useState } from 'react'
import type { TaskRecord, TaskStatus } from '../types'
import { TERMINAL_STATUSES } from '../types'
import { POLL } from '../polling'
import { api, sseUrl } from '../api/client'
import { useActiveConnectionId } from '../api/connections'

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
  const esRef = useRef<EventSource | null>(null)
  const pollRef = useRef<number | null>(null)
  const onChangeRef = useRef(onChange)
  const connId = useActiveConnectionId()
  onChangeRef.current = onChange

  useEffect(() => {
    setLive(null)
    setTimeline([])
    setChannel('off')
    if (!task.task_id || TERMINAL_STATUSES.includes(task.status)) return // 终态/空任务无事件可等

    const refresh = async () => {
      try {
        setLive(await api.task(task.task_id))
      } catch {
        /* 静默失败，保留现有展示 */
      }
    }

    const es = new EventSource(sseUrl(task.task_id))
    esRef.current = es
    setChannel('sse')
    const handle = (kind: TaskEventLine['kind']) => (e: MessageEvent) => {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(e.data)
      } catch {
        /* 忽略坏帧 */
      }
      // 订阅时任务已终态：服务端发完快照即关流，直接结束
      // （data.state 来自 JSON 边界，cast 到 TaskStatus 后做终态判定）
      if (data.snapshot === true && typeof data.state === 'string'
        && TERMINAL_STATUSES.includes(data.state as TaskStatus)) {
        es.close()
        esRef.current = null
        setChannel('off')
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
        es.close()
        esRef.current = null
        setChannel('off')
        refresh()
        onChangeRef.current?.()
      } else if (data.snapshot !== true) {
        refresh() // 状态/回调实质变化 → 拉最新记录（output/meta）
      }
    }
    es.addEventListener('status', handle('status'))
    es.addEventListener('callback', handle('callback'))
    es.addEventListener('done', handle('done'))
    es.onerror = () => {
      // SSE 不可用（代理/网络限制）→ 降级为 5s 轮询
      es.close()
      esRef.current = null
      setChannel('poll')
      pollRef.current = window.setInterval(refresh, POLL.tasks)
    }
    return () => {
      es.close()
      if (pollRef.current !== null) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      esRef.current = null
    }
    // 任务或连接变化时重建订阅（task.status 取打开时刻快照）。
    // connId：切换多 brain 连接时旧 EventSource 指向旧脑，必须关掉重订。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.task_id, connId])

  return { current: live ?? task, timeline, channel }
}
