import { useMemo, useState } from 'react'
import type { TaskRecord, TaskStatus } from '../types'
import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import { timeAgo } from '../utils'
import StatusBadge from '../components/StatusBadge'
import TaskDetailDrawer from '../components/TaskDetailDrawer'

const STATUSES: (TaskStatus | 'all')[] = [
  'all', 'running', 'awaiting_approval', 'pending', 'done', 'failed', 'lost', 'cancelled',
]

const DOT: Record<string, string> = {
  pending: 'bg-slate-300',
  running: 'bg-sky-400',
  done: 'bg-mint-500',
  failed: 'bg-rose-500',
  lost: 'bg-slate-400',
  cancelled: 'bg-zinc-400',
  awaiting_approval: 'bg-amber-400',
}

/** 任务全集：多维筛选 + 实时详情抽屉（SSE） */
export default function Tasks() {
  const { data: tasks, error, refresh } = usePolling<TaskRecord[]>(() => api.tasks(), 5000)
  const [status, setStatus] = useState<TaskStatus | 'all'>('all')
  const [role, setRole] = useState('all')
  const [worker, setWorker] = useState('all')
  const [q, setQ] = useState('')
  const [runningOnly, setRunningOnly] = useState(false)
  const [selected, setSelected] = useState<TaskRecord | null>(null)

  const roles = useMemo(
    () => [...new Set((tasks || []).map((t) => t.role))].sort(),
    [tasks],
  )
  const workers = useMemo(
    () => [...new Set((tasks || []).map((t) => t.worker_id || ''))].filter(Boolean).sort(),
    [tasks],
  )

  const filtered = useMemo(() => {
    let list = tasks || []
    if (status !== 'all') list = list.filter((t) => t.status === status)
    if (role !== 'all') list = list.filter((t) => t.role === role)
    if (worker !== 'all') list = list.filter((t) => t.worker_id === worker)
    if (runningOnly) list = list.filter((t) => t.status === 'running' || t.status === 'pending'
      || t.status === 'awaiting_approval')
    if (q.trim()) {
      const kw = q.trim().toLowerCase()
      list = list.filter((t) => t.task_id.toLowerCase().includes(kw)
        || t.prompt.toLowerCase().includes(kw))
    }
    return [...list].sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  }, [tasks, status, role, worker, runningOnly, q])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const t of tasks || []) c[t.status] = (c[t.status] || 0) + 1
    return c
  }, [tasks])

  return (
    <div>
      {/* 筛选栏 */}
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-sm p-4 mb-5 flex flex-wrap items-center gap-2 text-xs">
        <div className="flex flex-wrap gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`px-2.5 py-1 rounded-full font-medium transition ${
                status === s
                  ? 'bg-mint-600 text-white shadow-sm'
                  : 'bg-mint-50 text-slate-600 hover:bg-mint-100'
              }`}
            >
              {s === 'all' ? '全部' : s}
              {s !== 'all' && counts[s] ? ` (${counts[s]})` : ''}
            </button>
          ))}
        </div>
        <span className="text-slate-300">|</span>
        <Select value={role} onChange={setRole} options={roles} allLabel="全部角色" />
        <Select value={worker} onChange={setWorker} options={workers} allLabel="全部 Worker" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索任务 ID / prompt…"
          className="px-2.5 py-1 rounded-lg border border-mint-200/70 focus:outline-none focus:ring-2 focus:ring-mint-400 w-52"
        />
        <label className="flex items-center gap-1.5 text-slate-600 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={runningOnly}
            onChange={(e) => setRunningOnly(e.target.checked)}
            className="accent-mint-600"
          />
          仅进行中
        </label>
        <span className="ml-auto text-slate-400">
          {tasks ? `${filtered.length} / ${tasks.length} 条` : '加载中…'}
        </span>
      </div>

      {error && (
        <div className="text-rose-600 text-sm mb-3 bg-rose-50 border border-rose-200 rounded-xl p-2.5">
          大脑连接失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {/* 列表 */}
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-2">
        {filtered.length === 0 && !error && (
          <div className="text-sm text-slate-400 text-center py-10">没有匹配的任务</div>
        )}
        <div className="divide-y divide-slate-50">
          {filtered.map((t) => (
            <button
              key={t.task_id}
              onClick={() => setSelected(t)}
              className="w-full text-left px-3 py-2.5 rounded-xl hover:bg-mint-50/60 flex items-center gap-2.5 transition"
            >
              <span className={`w-2 h-2 rounded-full shrink-0 ${DOT[t.status] || 'bg-slate-300'}`} />
              <StatusBadge status={t.status} />
              {t.verdict?.verdict === 'block' && (
                <span className="text-amber-600 text-xs" title={t.verdict.reason}>⚠</span>
              )}
              <span className="font-mono text-xs text-slate-500 shrink-0">{t.task_id}</span>
              <span className="text-xs text-slate-400 shrink-0 hidden md:inline">{t.role}</span>
              <span className="text-sm text-slate-700 truncate flex-1">{t.prompt}</span>
              {t.meta?.cost_usd != null && (
                <span className="text-xs text-mint-700 shrink-0">${t.meta.cost_usd.toFixed(4)}</span>
              )}
              <span className="text-xs text-slate-400 shrink-0">{timeAgo(t.updated_at)}</span>
            </button>
          ))}
        </div>
      </div>

      <TaskDetailDrawer
        task={selected}
        open={selected != null}
        onClose={() => setSelected(null)}
        onChange={refresh}
      />
    </div>
  )
}

function Select({
  value, onChange, options, allLabel,
}: {
  value: string
  onChange: (v: string) => void
  options: string[]
  allLabel: string
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="px-2 py-1 rounded-lg border border-mint-200/70 bg-white focus:outline-none focus:ring-2 focus:ring-mint-400"
    >
      <option value="all">{allLabel}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}
