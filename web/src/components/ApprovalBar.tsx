import { useState } from 'react'
import type { ApprovalMeta, SecurityVerdict, TaskRecord } from '../types'
import { api } from '../api/client'

/**
 * 审批条：awaiting_approval 任务的裁决卡（置顶展示）。
 * 展示 security-guard 裁决理由 + 原指令，支持修订 prompt 后放行（重过检）或拒绝。
 */
export default function ApprovalBar({
  task,
  onDone,
}: {
  task: TaskRecord
  onDone?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [prompt, setPrompt] = useState('')

  // 审批详情在 meta.approval（指令拦截场景）；产物拦截则用 task.verdict
  const approval = (task.meta as (TaskRecord['meta'] & { approval?: ApprovalMeta }) | undefined)?.approval
  const verdict: SecurityVerdict | undefined = approval?.verdict ?? task.verdict
  const originalPrompt = approval?.original_prompt ?? task.prompt

  const approve = async () => {
    setBusy(true)
    setMsg(null)
    try {
      const r = await api.approveTask(task.task_id, prompt.trim() || undefined)
      if (r.approved === true) {
        setMsg({ ok: true, text: `已放行${r.dispatched_task_id ? `，新任务 ${r.dispatched_task_id}` : ''}` })
        setPrompt('')
        onDone?.()
      } else {
        setMsg({ ok: false, text: `放行未成功：${String(r.error ?? r.status)}` })
      }
    } catch (e) {
      setMsg({ ok: false, text: `放行失败: ${e instanceof Error ? e.message : String(e)}` })
    } finally {
      setBusy(false)
    }
  }

  const reject = async () => {
    if (!window.confirm(`确定拒绝并终结任务 [${task.task_id}] 吗？`)) return
    setBusy(true)
    setMsg(null)
    try {
      await api.rejectTask(task.task_id)
      setMsg({ ok: true, text: '已拒绝，任务终态 failed' })
      onDone?.()
    } catch (e) {
      setMsg({ ok: false, text: `拒绝失败: ${e instanceof Error ? e.message : String(e)}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-amber-50/80 border border-amber-200/90 rounded-xl p-4">
      <div className="flex items-center justify-between text-xs font-semibold text-amber-800 mb-2">
        <span>⏸ 安全裁决待人工审批 {verdict?.matched_rules?.length
          ? `（命中: ${verdict.matched_rules.join(', ')}）`
          : ''}</span>
        <span className="text-amber-600">security-guard</span>
      </div>
      {verdict && (
        <p className="text-xs text-slate-700 leading-relaxed">{verdict.reason}</p>
      )}
      {verdict?.suggestion && (
        <p className="text-xs text-slate-500 mt-1">建议: {verdict.suggestion}</p>
      )}
      <div className="mt-3">
        <div className="text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1">原指令</div>
        <pre className="bg-white/70 rounded-lg p-2.5 text-xs font-mono whitespace-pre-wrap max-h-28 overflow-auto border border-amber-200/60">{originalPrompt}</pre>
      </div>
      <div className="mt-2">
        <label className="text-[11px] font-bold text-slate-500 uppercase tracking-wider block mb-1">
          Prompt 修订（留空则按原指令放行；修订内容将重新过 security-guard）
        </label>
        <textarea
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="可附加放行约束，例如：仅允许在当前目录构建，禁止访问外网…"
          className="w-full text-xs font-mono p-2.5 rounded-lg border border-amber-200 focus:outline-none focus:ring-2 focus:ring-mint-500 bg-white"
        />
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        {msg && (
          <span className={`text-xs mr-auto ${msg.ok ? 'text-mint-700' : 'text-rose-600'}`}>{msg.text}</span>
        )}
        <button
          disabled={busy}
          onClick={reject}
          className="px-3 py-1.5 bg-white border border-slate-200 hover:bg-rose-50 hover:text-rose-600 text-xs font-medium text-slate-600 rounded-lg transition disabled:opacity-40"
        >
          拒绝任务
        </button>
        <button
          disabled={busy}
          onClick={approve}
          className="px-4 py-1.5 bg-mint-600 hover:bg-mint-500 text-white text-xs font-semibold rounded-lg shadow-sm transition disabled:opacity-40"
        >
          {prompt.trim() ? '修订并放行' : '放行'}
        </button>
      </div>
    </div>
  )
}
