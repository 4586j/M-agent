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
      <span className="text-xs text-slate-500 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 rounded-full bg-slate-100 overflow-hidden">
        {limit != null && (
          <div
            className={`h-full rounded-full transition-all ${over ? 'bg-rose-500' : pct > 80 ? 'bg-amber-500' : 'bg-mint-500'}`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      <span className={`text-xs font-mono shrink-0 ${over ? 'text-rose-600 font-semibold' : 'text-slate-600'}`}>
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
    <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-sm p-4 mb-5">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <span className="text-xs font-semibold text-slate-700 shrink-0">预算总闸</span>
        <BudgetRow label="全局" spent={budget.spent_total_usd} limit={budget.global_limit_usd} />
        <BudgetRow label="当日" spent={budget.spent_today_usd} limit={budget.daily_limit_usd} />
        <div className="ml-auto flex items-center gap-2">
          {msg && <span className="text-xs text-rose-600">{msg}</span>}
          {editing ? (
            <>
              <input
                value={gLimit}
                onChange={(e) => setGLimit(e.target.value)}
                placeholder="全局上限 USD"
                className="w-28 px-2 py-1 text-xs rounded-lg border border-mint-200/70 focus:outline-none focus:ring-2 focus:ring-mint-400"
              />
              <input
                value={dLimit}
                onChange={(e) => setDLimit(e.target.value)}
                placeholder="每日上限 USD"
                className="w-28 px-2 py-1 text-xs rounded-lg border border-mint-200/70 focus:outline-none focus:ring-2 focus:ring-mint-400"
              />
              <button
                onClick={save}
                className="px-2.5 py-1 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-700"
              >
                保存
              </button>
              <button
                onClick={() => setEditing(false)}
                className="px-2 py-1 text-xs text-slate-500 hover:text-slate-700"
              >
                取消
              </button>
            </>
          ) : (
            <button
              onClick={startEdit}
              className="px-2.5 py-1 text-xs rounded-lg bg-mint-50 text-mint-700 hover:bg-mint-100"
            >
              调整上限
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
