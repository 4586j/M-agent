import { useEffect, useMemo, useState, useRef } from 'react'
import type { TaskRecord, TaskStatus, BudgetSnapshot } from '../types'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'
import { useActiveConnectionScope } from '../api/connections'
import { api } from '../api/client'
import { fmtUsd, timeAgo } from '../utils'
import { statusDot } from '../types'
import StatusBadge from '../components/StatusBadge'
import TaskDetailDrawer from '../components/TaskDetailDrawer'
import BudgetBar from '../components/BudgetBar'

const STATUSES: (TaskStatus | 'all')[] = [
  'all', 'running', 'awaiting_approval', 'pending', 'done', 'failed', 'lost', 'cancelled',
]

/** 任务全集：多维筛选 + 实时详情抽屉（SSE） */
export default function Tasks() {
  const { data: tasks, error, refresh } = usePolling<TaskRecord[]>(() => api.tasks(), POLL.tasks)
  const { data: budget, refresh: refreshBudget } = usePolling<BudgetSnapshot>(
    () => api.budgets(), POLL.stats)
  const [status, setStatus] = useState<TaskStatus | 'all'>('all')
  const [role, setRole] = useState('all')
  const [worker, setWorker] = useState('all')
  const [q, setQ] = useState('')
  const [runningOnly, setRunningOnly] = useState(false)
  const [selected, setSelected] = useState<TaskRecord | null>(null)
  const connectionScope = useActiveConnectionScope()

  useEffect(() => {
    setSelected(null)
  }, [connectionScope])

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

  const statusRef = useRef<HTMLDivElement>(null)
  const [statusPill, setStatusPill] = useState<{ left: number; top: number; width: number; height: number } | null>(null)

  useEffect(() => {
    const updatePill = () => {
      if (!statusRef.current) return
      const activeBtn = statusRef.current.querySelector('[data-active="true"]') as HTMLElement | null
      if (activeBtn) {
        setStatusPill({
          left: activeBtn.offsetLeft,
          top: activeBtn.offsetTop,
          width: activeBtn.offsetWidth,
          height: activeBtn.offsetHeight,
        })
      }
    }
    updatePill()
    const t = setTimeout(updatePill, 30)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updatePill)
    if (statusRef.current) observer?.observe(statusRef.current)
    const active = statusRef.current?.querySelector('[data-active="true"]')
    if (active) observer?.observe(active)
    window.addEventListener('resize', updatePill)
    return () => {
      clearTimeout(t)
      observer?.disconnect()
      window.removeEventListener('resize', updatePill)
    }
  }, [status, counts])

  return (
    <div className="space-y-5">
      {/* 预算总闸：全局/当日已用 + 热调上限 */}
      <BudgetBar budget={budget} onChange={refreshBudget} />

      {/* 筛选栏 */}
      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] flex flex-wrap items-center gap-2.5 text-xs">
        <div ref={statusRef} className="flex flex-wrap gap-1 relative p-0.5 rounded-full bg-zinc-950/60 border border-white/[0.06]">
          {statusPill && (
            <div
              data-testid="task-status-indicator"
              className="absolute rounded-full pointer-events-none backdrop-blur-md bg-emerald-500/20 border border-emerald-500/40 shadow-sm transition-all duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] z-0"
              style={{
                left: `${statusPill.left}px`,
                top: `${statusPill.top}px`,
                width: `${statusPill.width}px`,
                height: `${statusPill.height}px`,
              }}
            />
          )}
          {STATUSES.map((s) => (
            <button
              key={s}
              type="button"
              data-active={status === s}
              onClick={() => setStatus(s)}
              className={`btn-press px-3 py-1 rounded-full font-medium transition duration-150 relative z-10 ${
                status === s
                  ? 'text-emerald-300 font-semibold'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {s === 'all' ? '全部' : s}
              {s !== 'all' && counts[s] ? ` (${counts[s]})` : ''}
            </button>
          ))}
        </div>

        <span className="text-zinc-700">|</span>

        <Select value={role} onChange={setRole} options={roles} allLabel="全部角色" />
        <Select value={worker} onChange={setWorker} options={workers} allLabel="全部 Worker" />

        <div className="relative">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索任务 ID / prompt…"
            className="px-3 py-1.5 rounded-xl border border-white/[0.08] bg-zinc-950/80 text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 focus:ring-1 focus:ring-emerald-500/30 w-56 font-mono text-xs transition"
          />
        </div>

        <label className="flex items-center gap-1.5 text-zinc-400 hover:text-zinc-200 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={runningOnly}
            onChange={(e) => setRunningOnly(e.target.checked)}
            className="accent-emerald-500"
          />
          仅进行中
        </label>

        <span className="ml-auto text-zinc-500 font-mono tabular-nums">
          {tasks ? `${filtered.length} / ${tasks.length} 条` : '加载中…'}
        </span>
      </div>

      {error && (
        <div className="text-rose-400 text-xs bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">
          大脑连接失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {/* 列表容器 */}
      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-2">
        {filtered.length === 0 && !error && (
          <div className="text-sm text-zinc-500 text-center py-12">没有匹配的任务</div>
        )}
        <div className="divide-y divide-white/[0.04]">
          {filtered.map((t) => (
            <button
              key={t.task_id}
              type="button"
              onClick={() => setSelected(t)}
              className="w-full text-left px-3.5 py-3 rounded-xl hover:bg-zinc-800/40 border border-transparent hover:border-white/[0.05] flex items-center gap-3 transition duration-150 group"
            >
              <span className={`w-2 h-2 rounded-full shrink-0 ${statusDot(t.status)}`} />
              <StatusBadge status={t.status} />
              {t.verdict?.verdict === 'block' && (
                <span className="text-amber-400 text-xs shrink-0" title={t.verdict.reason}>⚠</span>
              )}
              <span className="font-mono text-xs text-zinc-400 shrink-0">{t.task_id}</span>
              <span className="text-xs text-zinc-500 shrink-0 font-mono hidden md:inline">{t.role}</span>
              <span className="text-sm text-zinc-300 truncate flex-1 group-hover:text-zinc-100 transition font-sans">
                {t.prompt}
              </span>
              {t.meta?.cost_usd != null && (
                <span className="text-xs text-emerald-400 tabular-nums font-mono shrink-0">
                  {fmtUsd(t.meta.cost_usd)}
                </span>
              )}
              <span className="text-xs text-zinc-500 tabular-nums font-mono shrink-0">
                {timeAgo(t.updated_at)}
              </span>
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
      className="px-2.5 py-1.5 rounded-xl border border-white/[0.08] bg-zinc-950/80 text-zinc-300 focus:outline-none focus:border-emerald-500/40 text-xs font-mono"
    >
      <option value="all">{allLabel}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}
