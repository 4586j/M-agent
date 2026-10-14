import { useState } from 'react'
import type { BudgetSnapshot } from '../types'
import { api } from '../api/client'
import { fmtUsd } from '../utils'

/** 单条预算：标签 + 进度条 + 已用/上限（limit=null 显示「不限制」） */
function BudgetRow({ label, spent, limit }: {
  label: string
  spent: number
  limit: number | null
}) {
  const over = limit != null && spent >= limit
  const pct = limit != null && limit > 0 ? Math.min(100, (spent / limit) * 100) : 0
  return (
    <div className="flex items-center gap-2 min-w-45" data-testid={`budget-${label}`}>
      <span className="text-xs text-zinc-400 font-medium shrink-0">{label}</span>
      <div className="flex-1 h-1.5 rounded-full bg-zinc-800/90 overflow-hidden border border-white/[0.04]">
        {limit != null && (
          <div
            className={`h-full rounded-full transition-all duration-300 ${over ? 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.5)]' : pct > 80 ? 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.5)]' : 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.4)]'}`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      <span className={`text-xs font-mono tabular-nums shrink-0 ${over ? 'text-rose-400 font-semibold' : 'text-zinc-300'}`}>
        {fmtUsd(spent)} / {limit != null ? fmtUsd(limit) : '不限'}
      </span>
    </div>
  )
}

/** 全局/每日预算条：任务页顶部总闸视图 + 热调上限（超限后新派发会被 429 阻断） */
export default function BudgetBar({ budget, onChange }: {
  budget: BudgetSnapshot | null
  onChange: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [gLimit, setGLimit] = useState('')
  const [dLimit, setDLimit] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  if (!budget) return null

  const startEdit = () => {
    setGLimit(budget.global_limit_usd != null ? String(budget.global_limit_usd) : '')
    setDLimit(budget.daily_limit_usd != null ? String(budget.daily_limit_usd) : '')
    setMsg(null)
    setEditing(true)
  }

  const save = async () => {
    try {
      await api.putBudgets({
        global_limit_usd: gLimit.trim() ? Number(gLimit) : null,
        daily_limit_usd: dLimit.trim() ? Number(dLimit) : null,
      })
      setEditing(false)
      setMsg(null)
      onChange()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="bg-zinc-900/60 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-4 mb-5">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <span className="text-xs font-semibold text-zinc-200 shrink-0 flex items-center gap-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          预算总闸
        </span>
        <BudgetRow label="全局" spent={budget.spent_total_usd} limit={budget.global_limit_usd} />
        <BudgetRow label="当日" spent={budget.spent_today_usd} limit={budget.daily_limit_usd} />
        <div className="ml-auto flex items-center gap-2">
          {msg && <span className="text-xs text-rose-400 font-mono">{msg}</span>}
          {editing ? (
            <>
              <input
                value={gLimit}
                onChange={(e) => setGLimit(e.target.value)}
                placeholder="全局上限 USD"
                className="w-28 px-2 py-1 text-xs font-mono rounded-lg border border-white/[0.1] bg-zinc-950/80 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
              />
              <input
                value={dLimit}
                onChange={(e) => setDLimit(e.target.value)}
                placeholder="每日上限 USD"
                className="w-28 px-2 py-1 text-xs font-mono rounded-lg border border-white/[0.1] bg-zinc-950/80 text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
              />
              <button
                onClick={save}
                className="btn-press px-2.5 py-1 text-xs font-semibold rounded-lg bg-emerald-500 text-zinc-950 hover:bg-emerald-400 transition"
              >
                保存
              </button>
              <button
                onClick={() => setEditing(false)}
                className="btn-press px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 transition"
              >
                取消
              </button>
            </>
          ) : (
            <button
              onClick={startEdit}
              className="btn-press px-2.5 py-1 text-xs font-medium rounded-lg bg-zinc-800/80 hover:bg-zinc-800 text-zinc-300 border border-white/[0.06] hover:border-emerald-500/30 transition"
            >
              调整上限
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
