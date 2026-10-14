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

  const loadColor = worker.load >= 4 ? 'bg-rose-400' : worker.load >= 2 ? 'bg-amber-400' : 'bg-emerald-400'
  const loadPct = Math.min(100, Math.max(10, (worker.load / 4) * 100))
  const caps = Object.keys(worker.capabilities || {})

  return (
    <div className="spotlight-card bg-zinc-900/50 hover:bg-zinc-900/80 border border-white/[0.07] hover:border-emerald-500/30 rounded-2xl shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] transition-all duration-200 overflow-hidden">
      <button
        onClick={toggle}
        className="w-full text-left p-4 flex items-center justify-between hover:bg-zinc-800/30 transition"
      >
        <div className="space-y-2 flex-1 min-w-0 pr-4">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`inline-block w-2 h-2 rounded-full ${alive ? 'bg-emerald-400 animate-pulse shadow-[0_0_8px_rgba(52,211,153,0.6)]' : 'bg-zinc-600'}`} />
            <span className="font-semibold text-zinc-100 font-mono text-sm tracking-tight">{worker.worker_id}</span>
            <span className="text-xs bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 px-2 py-0.5 rounded-md font-mono">{worker.role}</span>
            {alive ? (
              <span className="text-[10px] font-mono text-emerald-400/80 bg-emerald-500/10 px-1.5 py-0.2 rounded border border-emerald-500/20">ONLINE</span>
            ) : (
              <span className="text-[10px] font-mono text-zinc-500 bg-zinc-800/80 px-1.5 py-0.2 rounded border border-white/[0.05]">OFFLINE</span>
            )}
          </div>
          <div className="text-xs text-zinc-400 flex items-center gap-2 flex-wrap font-mono">
            <span>{worker.host}:{worker.port}</span>
            <span className="text-zinc-600">·</span>
            <span className="tabular-nums">负载 {worker.load}</span>
            <span className="text-zinc-600">·</span>
            <span className="tabular-nums text-zinc-500">心跳 {timeAgo(worker.last_heartbeat)}</span>
          </div>
          {/* 可视化负载槽位进度条 */}
          <div className="w-full max-w-xs flex items-center gap-2 pt-0.5">
            <div className="flex-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden border border-white/[0.04]">
              <div
                className={`h-full rounded-full transition-all duration-300 ${loadColor}`}
                style={{ width: `${loadPct}%` }}
              />
            </div>
            <span className="text-[10px] font-mono text-zinc-500 tabular-nums">槽位 {worker.load}/4</span>
          </div>
          {/* Capabilities 技能标签组 */}
          {caps.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {caps.map((k) => (
                <span
                  key={k}
                  className="text-[11px] font-mono px-2 py-0.5 rounded bg-zinc-800/80 text-zinc-300 border border-white/[0.06]"
                >
                  {k}
                </span>
              ))}
            </div>
          )}
        </div>
        <span className="text-zinc-500 text-sm font-bold pl-2">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-white/[0.06] p-4 bg-zinc-950/60">
          {loading ? (
            <div className="text-sm text-zinc-500 font-mono">加载任务…</div>
          ) : tasks ? (
            <TaskList tasks={tasks} onChange={refreshTasks} />
          ) : null}
        </div>
      )}
    </div>
  )
}
