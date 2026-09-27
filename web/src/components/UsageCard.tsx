import type { TaskMeta } from '../types'

/** 用量四格卡：tokens / turns / 成本 / 缓存命中率（数据缺失显示 —） */
export default function UsageCard({ meta }: { meta?: TaskMeta }) {
  const usage = meta?.usage
  const tokens = sumTokens(usage)
  const cacheHit = cacheHitRate(usage)
  if (tokens == null && meta?.num_turns == null && meta?.cost_usd == null) return null
  return (
    <div className="bg-mint-50/50 border border-mint-100 rounded-xl p-4">
      <h4 className="text-xs font-bold text-slate-600 mb-2">执行消耗度量</h4>
      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <Cell label="Tokens" value={tokens != null ? tokens.toLocaleString() : '—'} />
        <Cell label="Turns" value={meta?.num_turns != null ? `${meta.num_turns} 轮` : '—'} />
        <Cell label="成本" value={meta?.cost_usd != null ? `$${meta.cost_usd.toFixed(4)}` : '—'} accent />
        <Cell
          label="缓存命中"
          value={cacheHit != null ? `${Math.round(cacheHit * 100)}%` : '—'}
        />
      </div>
      {meta?.model && (
        <div className="mt-2 text-[11px] text-slate-400 font-mono">model: {meta.model}</div>
      )}
    </div>
  )
}

function Cell({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="bg-white p-2 rounded-lg border border-mint-100/60">
      <div className="text-slate-400">{label}</div>
      <div className={`font-bold mt-0.5 ${accent ? 'text-mint-700' : 'text-slate-700'}`}>{value}</div>
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
