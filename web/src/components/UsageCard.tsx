import type { TaskMeta } from '../types'
import { fmtUsd } from '../utils'

/** 用量四格卡：tokens / turns / 成本 / 缓存命中率（数据缺失显示 —） */
export default function UsageCard({ meta }: { meta?: TaskMeta }) {
  const usage = meta?.usage
  const tokens = sumTokens(usage)
  const cacheHit = cacheHitRate(usage)
  if (tokens == null && meta?.num_turns == null && meta?.cost_usd == null) return null
  return (
    <div className="bg-zinc-900/60 backdrop-blur-xl border border-white/[0.07] rounded-xl p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]">
      <h4 className="text-xs font-bold text-zinc-300 mb-2.5 flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-indigo-400" />
        执行消耗度量
      </h4>
      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <Cell label="Tokens" value={tokens != null ? tokens.toLocaleString() : '—'} />
        <Cell label="Turns" value={meta?.num_turns != null ? `${meta.num_turns} 轮` : '—'} />
        <Cell label="成本" value={meta?.cost_usd != null ? fmtUsd(meta.cost_usd) : '—'} accent />
        <Cell
          label="缓存命中"
          value={cacheHit != null ? `${Math.round(cacheHit * 100)}%` : '—'}
        />
      </div>
      {meta?.model && (
        <div className="mt-2.5 text-[11px] text-zinc-400 font-mono">model: {meta.model}</div>
      )}
    </div>
  )
}

function Cell({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="bg-zinc-950/70 p-2.5 rounded-lg border border-white/[0.05] transition hover:border-white/10">
      <div className="text-zinc-400 text-[11px] font-medium">{label}</div>
      <div className={`font-mono font-bold text-sm mt-1 tabular-nums ${accent ? 'text-emerald-400' : 'text-zinc-100'}`}>{value}</div>
    </div>
  )
}

function sumTokens(usage?: TaskMeta['usage']): number | null {
  if (!usage) return null
  const keys = ['input_tokens', 'output_tokens',
    'cache_read_input_tokens', 'cache_creation_input_tokens'] as const
  let total = 0
  let has = false
  for (const k of keys) {
    const v = usage[k]
    if (typeof v === 'number' && isFinite(v)) {
      total += v
      has = true
    }
  }
  return has ? total : null
}

/** 缓存命中率 = cache_read / (input + cache_read + cache_creation)；无缓存数据返回 null */
function cacheHitRate(usage?: TaskMeta['usage']): number | null {
  if (!usage) return null
  const read = usage.cache_read_input_tokens
  const input = usage.input_tokens
  const creation = usage.cache_creation_input_tokens
  if (typeof read !== 'number' || read <= 0) return null
  const denom = read + (typeof input === 'number' ? input : 0)
    + (typeof creation === 'number' ? creation : 0)
  return denom > 0 ? read / denom : null
}
