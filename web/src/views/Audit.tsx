import { useState } from 'react'
import type { AuditEntry } from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'

const STAGES = ['', 'dispatch', 'product_check', 'approve']
const DECISIONS = ['', 'block', 'warn', 'pass', 'force', 'approve', 'reject']

const DECISION_CLS: Record<string, string> = {
  block: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  warn: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  pass: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  force: 'bg-amber-500/20 text-amber-200 border border-amber-500/30',
  approve: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  reject: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
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
      {/* 头部筛选栏 */}
      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-3.5 flex flex-wrap items-center gap-2.5 text-xs">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
          <span className="text-zinc-200 font-bold uppercase tracking-wider">安全裁决流水</span>
        </div>
        <span className="text-zinc-700">|</span>
        <select
          value={stage}
          onChange={(e) => setStage(e.target.value)}
          className="px-2.5 py-1.5 rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-300 focus:outline-none focus:border-emerald-500/40 text-xs"
        >
          <option value="">全部环节</option>
          {STAGES.filter(Boolean).map((s) => <option key={s} value={s}>{STAGE_LABEL[s] || s}</option>)}
        </select>
        <select
          value={decision}
          onChange={(e) => setDecision(e.target.value)}
          className="px-2.5 py-1.5 rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-300 focus:outline-none focus:border-emerald-500/40 text-xs"
        >
          <option value="">全部裁决</option>
          {DECISIONS.filter(Boolean).map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <span className="text-zinc-400 font-mono tabular-nums">
          {entries.length} 条
          {blockCount > 0 && (
            <span className="text-rose-400 font-semibold ml-1.5">（含阻断 {blockCount}）</span>
          )}
        </span>
        <button
          type="button"
          onClick={() => refresh()}
          className="ml-auto text-emerald-400 hover:text-emerald-300 hover:underline font-medium transition"
        >
          刷新
        </button>
      </div>

      {error && (
        <div className="text-rose-400 text-xs bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">
          加载失败：{error}（/audit 端点需要 brain_api 支持；确认 runtime 已包含 audit_api 挂载）
        </div>
      )}

      {/* 流水记录卡片 */}
      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-2">
        {entries.length === 0 && !error ? (
          <div className="text-sm text-zinc-500 text-center py-12">
            暂无审计记录——安全裁决（关卡①②/审批）发生时会自动落盘于此
          </div>
        ) : (
          <div className="divide-y divide-white/[0.04]">
            {entries.map((e, i) => (
              <div key={i}>
                <button
                  type="button"
                  onClick={() => setExpanded(expanded === i ? null : i)}
                  className="w-full text-left px-3.5 py-3 rounded-xl hover:bg-zinc-800/40 flex items-center gap-2.5 text-xs transition duration-150 group"
                >
                  <span className="font-mono text-zinc-500 tabular-nums shrink-0">
                    {e.timestamp.replace('T', ' ').slice(0, 19)}
                  </span>
                  <span className={`px-2 py-0.5 rounded-md font-mono text-[11px] font-bold shrink-0 ${DECISION_CLS[e.decision] || 'bg-zinc-800 text-zinc-400'}`}>
                    {e.decision}
                  </span>
                  <span className="text-zinc-400 shrink-0 font-medium">{STAGE_LABEL[e.stage] || e.stage}</span>
                  {e.task_id && <span className="font-mono text-zinc-400 shrink-0">{e.task_id}</span>}
                  {e.matched_rules.length > 0 && (
                    <span className="text-amber-400 font-mono text-[11px] shrink-0">
                      命中: {e.matched_rules.join(', ')}
                    </span>
                  )}
                  <span className="text-zinc-400 truncate flex-1 group-hover:text-zinc-200 transition font-sans" title={e.reason}>
                    {e.reason || '—'}
                  </span>
                  {e.llm_enhanced && (
                    <span className="text-indigo-400 font-mono text-xs shrink-0" title="本次裁决启用了 LLM 增强">
                      LLM✦
                    </span>
                  )}
                  {e.operator && (
                    <span className="text-zinc-500 shrink-0" title={`操作者: ${e.operator}`}>👤</span>
                  )}
                </button>
                {expanded === i && (
                  <pre className="mx-3 mb-3 bg-[#050507] text-zinc-200 rounded-xl p-4 text-xs font-mono overflow-auto border border-white/[0.08] custom-scroll">
                    {JSON.stringify(e, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
