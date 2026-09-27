import type { Stats } from '../types'

/** 统计条：四张 mint 白卡（顶部嫩绿渐变边条），对齐原型规格 */
export default function StatBar({ stats, cacheHit }: {
  stats: Stats | null
  cacheHit: number | null
}) {
  if (!stats) return <div className="text-slate-400 text-sm">加载统计中…</div>
  const ts = stats.task_status || {}
  const running = (ts.running || 0) + (ts.pending || 0) + (ts.awaiting_approval || 0)
  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
      <Card
        label="活跃 / 总节点"
        badge="健康"
        badgeCls="text-mint-600 bg-mint-50"
        value={`${stats.worker_active} / ${stats.worker_total}`}
        hint={stats.mode === 'remote' ? `remote · 心跳超时 ${stats.heartbeat_timeout}s` : stats.mode}
      />
      <Card
        label="任务全量"
        badge="实时"
        badgeCls="text-sky-600 bg-sky-50"
        value={String(stats.task_total)}
        hint={
          <span className="flex items-center gap-2">
            <span className="text-sky-600 font-medium">● 进行 {running}</span>
            <span className="text-amber-500 font-semibold">⏸ 待批 {ts.awaiting_approval || 0}</span>
            <span className="text-rose-500 font-medium">✕ 失败 {ts.failed || 0}</span>
          </span>
        }
      />
      <Card
        label="累计消耗"
        badge="Cost"
        badgeCls="text-mint-600 bg-mint-50"
        value={stats.cost?.total?.cost_usd != null ? `$${stats.cost.total.cost_usd.toFixed(4)}` : '—'}
        hint={stats.cost?.total?.tokens != null ? `${stats.cost.total.tokens.toLocaleString()} tokens` : '暂无 meta 数据'}
      />
      <Card
        label="提示词缓存命中率"
        badge="Cache"
        badgeCls="text-indigo-600 bg-indigo-50"
        value={cacheHit != null ? `${Math.round(cacheHit * 100)}%` : '—'}
        hint={cacheHit != null ? '命中越多单任务越省' : '暂无缓存数据'}
      />
    </div>
  )
}

function Card({ label, badge, badgeCls, value, hint }: {
  label: string
  badge?: string
  badgeCls?: string
  value: string
  hint?: React.ReactNode
}) {
  return (
    <div className="bg-white p-5 rounded-2xl border border-mint-100/80 shadow-mint-card relative overflow-hidden hover:-translate-y-0.5 transition duration-200">
      <div className="absolute top-0 left-0 right-0 h-0.5 bg-gradient-to-r from-mint-300/0 via-mint-400/60 to-mint-300/0" />
      <div className="flex items-center justify-between text-slate-500 text-xs font-medium">
        <span>{label}</span>
        {badge && <span className={`px-2 py-0.5 rounded-md font-semibold ${badgeCls}`}>{badge}</span>}
      </div>
      <div className="mt-2 text-2xl font-bold text-slate-800">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  )
}
