import { useState } from 'react'
import type { AuditEntry } from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'

const STAGES = ['', 'dispatch', 'product_check', 'approve']
const DECISIONS = ['', 'block', 'warn', 'pass', 'force', 'approve', 'reject']

const DECISION_CLS: Record<string, string> = {
  block: 'bg-rose-100 text-rose-700',
  warn: 'bg-amber-100 text-amber-700',
  pass: 'bg-mint-100 text-mint-700',
  force: 'bg-amber-100 text-amber-800',
  approve: 'bg-mint-100 text-mint-700',
  reject: 'bg-rose-100 text-rose-700',
}

const STAGE_LABEL: Record<string, string> = {
  dispatch: '指令关卡①',
  product_check: '产物关卡②',
  approve: '人工审批',
}

/** 审计视图：安全裁决流水（只读，block/force 高亮） */
export default function Audit() {
  const [stage, setStage] = useState('')
  const [decision, setDecision] = useState('')
  const { data, error, refresh } = usePolling(
    () => api.audit({ stage: stage || undefined, decision: decision || undefined, limit: 200 }),
    10000,
  )
  const [expanded, setExpanded] = useState<number | null>(null)

  const entries = data ?? []
  const blockCount = entries.filter((e) => e.decision === 'block' || e.decision === 'reject').length

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-sm p-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-400 font-bold uppercase tracking-wider">安全裁决流水</span>
        <span className="text-slate-300">|</span>
        <select value={stage} onChange={(e) => setStage(e.target.value)}
          className="px-2 py-1 rounded-lg border border-mint-200/70 bg-white">
          <option value="">全部环节</option>
          {STAGES.filter(Boolean).map((s) => <option key={s} value={s}>{STAGE_LABEL[s] || s}</option>)}
        </select>
        <select value={decision} onChange={(e) => setDecision(e.target.value)}
          className="px-2 py-1 rounded-lg border border-mint-200/70 bg-white">
          <option value="">全部裁决</option>
          {DECISIONS.filter(Boolean).map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <span className="text-slate-400">{entries.length} 条{blockCount > 0 && (
          <span className="text-rose-600 font-semibold ml-1">（含阻断 {blockCount}）</span>
        )}</span>
        <button onClick={() => refresh()} className="ml-auto text-mint-700 hover:underline font-medium">刷新</button>
      </div>

      {error && (
        <div className="text-rose-600 text-sm bg-rose-50 border border-rose-200 rounded-xl p-2.5">
          加载失败：{error}（/audit 端点需要 brain_api 支持；确认 runtime 已包含 audit_api 挂载）
        </div>
      )}

      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-2">
        {entries.length === 0 && !error ? (
          <div className="text-sm text-slate-400 text-center py-10">
            暂无审计记录——安全裁决（关卡①②/审批）发生时会自动落盘于此
          </div>
        ) : (
          <div className="divide-y divide-slate-50">
            {entries.map((e, i) => (
              <div key={i}>
                <button
                  onClick={() => setExpanded(expanded === i ? null : i)}
                  className="w-full text-left px-3 py-2.5 rounded-xl hover:bg-mint-50/50 flex items-center gap-2.5 text-xs transition"
                >
                  <span className="font-mono text-slate-400 shrink-0">{e.timestamp.replace('T', ' ').slice(0, 19)}</span>
                  <span className={`px-2 py-0.5 rounded font-bold shrink-0 ${DECISION_CLS[e.decision] || 'bg-slate-100 text-slate-600'}`}>
                    {e.decision}
                  </span>
                  <span className="text-slate-500 shrink-0">{STAGE_LABEL[e.stage] || e.stage}</span>
                  {e.task_id && <span className="font-mono text-slate-500 shrink-0">{e.task_id}</span>}
                  {e.matched_rules.length > 0 && (
                    <span className="text-amber-700 shrink-0">命中: {e.matched_rules.join(', ')}</span>
                  )}
                  <span className="text-slate-500 truncate flex-1" title={e.reason}>{e.reason || '—'}</span>
                  {e.llm_enhanced && <span className="text-violet-600 shrink-0" title="本次裁决启用了 LLM 增强">LLM✦</span>}
                  {e.operator && <span className="text-slate-400 shrink-0" title={`操作者: ${e.operator}`}>👤</span>}
                </button>
                {expanded === i && (
                  <pre className="mx-3 mb-2 bg-slate-50 rounded-xl p-3 text-xs overflow-auto">{JSON.stringify(e, null, 2)}</pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
