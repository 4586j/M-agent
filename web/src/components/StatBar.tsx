import type { Stats } from '../types'
import { fmtUsd } from '../utils'

/** 统计条：四张 Bento 指标卡（带鼠标跟随聚光灯与层叠淡入） */
export default function StatBar({ stats, cacheHit }: {
  stats: Stats | null
  cacheHit: number | null
}) {
  if (!stats) return <div className="text-zinc-500 text-sm font-mono">加载统计中…</div>
  const ts = stats.task_status || {}
  const running = (ts.running || 0) + (ts.pending || 0) + (ts.awaiting_approval || 0)
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <Card
        staggerIndex={0}
        label="活跃 / 总节点"
        badge="健康"
        badgeCls="text-emerald-400 bg-emerald-500/10 border border-emerald-500/20"
        value={`${stats.worker_active} / ${stats.worker_total}`}
        hint={stats.mode === 'remote' ? `remote · 心跳超时 ${stats.heartbeat_timeout}s` : stats.mode}
        icon={
          <svg className="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        }
      />
      <Card
        staggerIndex={1}
        label="任务全量"
        badge="实时"
        badgeCls="text-sky-400 bg-sky-500/10 border border-sky-500/20"
        value={String(stats.task_total)}
        hint={
          <span className="flex items-center gap-2">
            <span className="text-sky-400 font-medium">● 进行 {running}</span>
            <span className="text-amber-400 font-semibold">⏸ 待批 {ts.awaiting_approval || 0}</span>
            <span className="text-rose-400 font-medium">✕ 失败 {ts.failed || 0}</span>
          </span>
        }
        icon={
          <svg className="w-4 h-4 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
        }
      />
      <Card
        staggerIndex={2}
        label="累计消耗"
        badge="Cost"
        badgeCls="text-emerald-400 bg-emerald-500/10 border border-emerald-500/20"
        value={stats.cost?.total?.cost_usd != null ? fmtUsd(stats.cost.total.cost_usd) : '—'}
        hint={stats.cost?.total?.tokens != null ? `${stats.cost.total.tokens.toLocaleString()} tokens` : '暂无 meta 数据'}
        icon={
          <svg className="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        }
      />
      <Card
        staggerIndex={3}
        label="提示词缓存命中率"
        badge="Cache"
        badgeCls="text-indigo-400 bg-indigo-500/10 border border-indigo-500/20"
        value={cacheHit != null ? `${Math.round(cacheHit * 100)}%` : '—'}
        hint={cacheHit != null ? '命中越多单任务越省' : '暂无缓存数据'}
        icon={
          <svg className="w-4 h-4 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
        }
      />
    </div>
  )
}

function Card({ label, badge, badgeCls, value, hint, icon, staggerIndex = 0 }: {
  label: string
  badge?: string
  badgeCls?: string
  value: string
  hint?: React.ReactNode
  icon?: React.ReactNode
  staggerIndex?: number
}) {
  return (
    <div
      style={{ '--stagger': staggerIndex } as React.CSSProperties}
      className="spotlight-card stagger-item bg-zinc-900/60 backdrop-blur-xl p-5 rounded-2xl border border-white/[0.07] hover:border-emerald-500/30 transition-all duration-200 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] relative overflow-hidden group"
    >
      <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-emerald-500/30 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
      <div className="flex items-center justify-between text-zinc-400 text-xs font-medium">
        <span>{label}</span>
        <div className="flex items-center gap-1.5">
          {badge && (
            <span className={`px-2 py-0.5 rounded-md text-[11px] font-semibold tracking-wide font-mono ${badgeCls}`}>
              {badge}
            </span>
          )}
          {icon && (
            <div className="p-1.5 rounded-lg bg-zinc-800/80 border border-white/[0.05]">
              {icon}
            </div>
          )}
        </div>
      </div>
      <div className="mt-2 text-2xl font-bold text-zinc-100 tabular-nums tracking-tight font-mono">
        {value}
      </div>
      {hint && <div className="mt-1.5 text-xs text-zinc-400 tabular-nums font-mono">{hint}</div>}
    </div>
  )
}
