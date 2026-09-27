import type { CostStats } from '../types'

/** 成本看板：按日（近 7 天）条形 + 按模型占比列表，数据来自 /stats.cost */
export default function CostPanel({ cost }: { cost?: CostStats }) {
  if (!cost) return null
  const days = cost.by_day
  const models = Object.entries(cost.by_model)
  const maxDayCost = Math.max(0, ...days.map((d) => d.cost_usd ?? 0))
  const maxModelCost = Math.max(0, ...models.map(([, b]) => b.cost_usd ?? 0))
  return (
    <div className="bg-white rounded-lg shadow p-4 mb-6">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-base font-semibold">成本看板</h2>
        <span className="text-xs text-slate-400">
          累计 {cost.total.tasks} 任务 · tokens {fmtNum(cost.total.tokens)} · 成本 {fmtUsd(cost.total.cost_usd)}
        </span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <div className="text-xs text-slate-500 mb-2">近 7 天按日</div>
          {days.length === 0 ? (
            <Empty text="近 7 天无任务" />
          ) : (
            <div className="space-y-1.5">
              {days.map((d) => (
                <div key={d.date} className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-slate-500 w-14 shrink-0">{d.date.slice(5)}</span>
                  <div className="flex-1 h-2.5 bg-slate-100 rounded overflow-hidden">
                    <div
                      className="h-full bg-sky-500 rounded"
                      style={{ width: `${barPct(d.cost_usd, maxDayCost)}%` }}
                    />
                  </div>
                  <span className="text-slate-500 w-32 text-right shrink-0">
                    {fmtNum(d.tokens)} tok · {fmtUsd(d.cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="text-xs text-slate-500 mb-2">按模型</div>
          {models.length === 0 ? (
            <Empty text="暂无模型维度数据（meta 缺失）" />
          ) : (
            <div className="space-y-1.5">
              {models.map(([model, b]) => (
                <div key={model} className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-slate-600 w-36 truncate shrink-0" title={model}>{model}</span>
                  <div className="flex-1 h-2.5 bg-slate-100 rounded overflow-hidden">
                    <div
                      className="h-full bg-emerald-500 rounded"
                      style={{ width: `${barPct(b.cost_usd, maxModelCost)}%` }}
                    />
                  </div>
                  <span className="text-slate-500 w-32 text-right shrink-0">
                    {b.tasks} 任务 · {fmtUsd(b.cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function barPct(v: number | null, max: number): number {
  if (v == null || max <= 0) return 0
  return Math.max(2, Math.round((v / max) * 100))
}

function fmtUsd(v: number | null): string {
  if (v == null) return '-'
  return `$${v >= 0.01 || v === 0 ? v.toFixed(2) : v.toFixed(4)}`
}

function fmtNum(v: number | null): string {
  if (v == null) return '-'
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return String(v)
}

function Empty({ text }: { text: string }) {
  return <div className="text-xs text-slate-400">{text}</div>
}
