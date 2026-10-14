import type { CostStats } from '../types'
import { fmtNum, fmtUsd } from '../utils'

/** 成本看板：按日（近 7 天）条形 + 按模型占比列表，数据来自 /stats.cost */
export default function CostPanel({ cost }: { cost?: CostStats }) {
  if (!cost) return null
  const days = cost.by_day
  const models = Object.entries(cost.by_model)
  const maxDayCost = Math.max(0, ...days.map((d) => d.cost_usd ?? 0))
  const maxModelCost = Math.max(0, ...models.map(([, b]) => b.cost_usd ?? 0))
  return (
    <div className="bg-zinc-900/60 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5 mb-6">
      <div className="flex items-baseline justify-between mb-4">
        <h2 className="text-base font-semibold text-zinc-100 tracking-tight flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          成本看板
        </h2>
        <span className="text-xs text-zinc-400 font-mono tabular-nums">
          累计 {cost.total.tasks} 任务 · tokens {fmtNum(cost.total.tokens)} · 成本 {fmtUsd(cost.total.cost_usd)}
        </span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <div className="text-xs text-zinc-400 mb-2.5 font-medium flex items-center gap-1.5">
            <span className="w-1 h-1 rounded-full bg-emerald-400" />
            近 7 天按日
          </div>
          {days.length === 0 ? (
            <Empty text="近 7 天无任务" />
          ) : (
            <div className="space-y-2">
              {days.map((d) => (
                <div key={d.date} className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-zinc-400 w-14 shrink-0 tabular-nums">{d.date.slice(5)}</span>
                  <div className="flex-1 h-2 bg-zinc-950 rounded-full overflow-hidden border border-white/[0.04]">
                    <div
                      className="h-full bg-gradient-to-r from-emerald-500 to-emerald-400 rounded-full transition-all duration-300"
                      style={{ width: `${barPct(d.cost_usd, maxDayCost)}%` }}
                    />
                  </div>
                  <span className="text-zinc-300 w-32 text-right shrink-0 tabular-nums font-mono text-[11px]">
                    {fmtNum(d.tokens)} tok · {fmtUsd(d.cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="text-xs text-zinc-400 mb-2.5 font-medium flex items-center gap-1.5">
            <span className="w-1 h-1 rounded-full bg-indigo-400" />
            按模型
          </div>
          {models.length === 0 ? (
            <Empty text="暂无模型维度数据（meta 缺失）" />
          ) : (
            <div className="space-y-2">
              {models.map(([model, b]) => (
                <div key={model} className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-zinc-300 w-36 truncate shrink-0 text-[11px]" title={model}>{model}</span>
                  <div className="flex-1 h-2 bg-zinc-950 rounded-full overflow-hidden border border-white/[0.04]">
                    <div
                      className="h-full bg-gradient-to-r from-indigo-500 to-indigo-400 rounded-full transition-all duration-300"
                      style={{ width: `${barPct(b.cost_usd, maxModelCost)}%` }}
                    />
                  </div>
                  <span className="text-zinc-300 w-32 text-right shrink-0 tabular-nums font-mono text-[11px]">
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

function Empty({ text }: { text: string }) {
  return <div className="text-xs text-zinc-500 font-mono">{text}</div>
}
