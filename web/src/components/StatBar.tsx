import type { Stats } from '../types'

export default function StatBar({ stats }: { stats: Stats | null }) {
  if (!stats) return <div className="text-slate-400 text-sm mb-6">加载统计中…</div>
  const ts = stats.task_status || {}
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
      <Card label="运行模式" value={stats.mode} />
      <Card label="活跃 / 总节点" value={`${stats.worker_active} / ${stats.worker_total}`} />
      <Card label="任务总数" value={String(stats.task_total)} />
      <div className="bg-white rounded-lg shadow p-3">
        <div className="text-xs text-slate-500 mb-1">任务状态分布</div>
        <div className="flex flex-wrap gap-1.5 text-xs">
          {Object.entries(ts).map(([k, v]) => (
            <span key={k} className="px-2 py-0.5 rounded bg-slate-100">{k}: {v}</span>
          ))}
          {Object.keys(ts).length === 0 && <span className="text-slate-400">无任务</span>}
        </div>
      </div>
    </div>
  )
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white rounded-lg shadow p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-lg font-semibold mt-1">{value}</div>
    </div>
  )
}
