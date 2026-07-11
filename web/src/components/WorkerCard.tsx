import { useState } from 'react'
import type { WorkerInfo, TaskRecord } from '../types'
import { api } from '../api/client'
import { timeAgo } from '../utils'
import TaskList from './TaskList'

export default function WorkerCard({
  worker,
  heartbeatTimeout,
}: {
  worker: WorkerInfo
  heartbeatTimeout: number
}) {
  const [open, setOpen] = useState(false)
  const [tasks, setTasks] = useState<TaskRecord[] | null>(null)
  const [loading, setLoading] = useState(false)

  const ago = Math.max(0, Math.round(Date.now() / 1000 - worker.last_heartbeat))
  const alive = ago <= heartbeatTimeout

  const refreshTasks = async () => setTasks(await api.tasks(worker.worker_id))

  const toggle = async () => {
    if (!open && tasks === null) {
      setLoading(true)
      try {
        await refreshTasks()
      } finally {
        setLoading(false)
      }
    }
    setOpen(!open)
  }

  return (
    <div className="bg-white rounded-lg shadow border border-slate-200 overflow-hidden">
      <button
        onClick={toggle}
        className="w-full text-left p-4 flex items-center justify-between hover:bg-slate-50"
      >
        <div>
          <div className="flex items-center gap-2">
            <span className={`inline-block w-2 h-2 rounded-full ${alive ? 'bg-emerald-500' : 'bg-slate-300'}`} />
            <span className="font-semibold">{worker.worker_id}</span>
            <span className="text-xs text-slate-400">{worker.role}</span>
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {worker.host}:{worker.port} · load {worker.load} · 心跳 {timeAgo(worker.last_heartbeat)}
          </div>
        </div>
        <span className="text-slate-400">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-slate-100 p-4 bg-slate-50/50">
          {loading ? (
            <div className="text-sm text-slate-400">加载任务…</div>
          ) : tasks ? (
            <TaskList tasks={tasks} onChange={refreshTasks} />
          ) : null}
        </div>
      )}
    </div>
  )
}
