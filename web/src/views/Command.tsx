import { useState } from 'react'
import { api } from '../api/client'
import type { DispatchResult, TaskRecord } from '../types'
import { usePolling } from '../hooks/usePolling'
import { formatTime, timeAgo } from '../utils'
import StatusBadge from '../components/StatusBadge'

export default function Command() {
  const [prompt, setPrompt] = useState('')
  const [role, setRole] = useState('executor')
  const [model, setModel] = useState('')
  const [ctx, setCtx] = useState('')
  const [result, setResult] = useState<DispatchResult | null>(null)
  const [busy, setBusy] = useState(false)

  // 历史命令从后端 /tasks 取（持久化，含发布时间/节点/结果）；5s 轮询
  const { data: tasks, refresh } = usePolling<TaskRecord[]>(() => api.tasks(), 5000)

  const send = async (force = false) => {
    if (!prompt.trim()) return
    setBusy(true)
    setResult(null)
    try {
      const res = await api.dispatch({
        prompt, role,
        context_memory: ctx || null,
        model: model || null,
        force,
      })
      setResult(res)
      if (res.status !== 'blocked') refresh()
    } catch (e) {
      setResult({
        task_id: '', status: 'failed', output: null, artifacts: [],
        error: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setBusy(false)
    }
  }

  const history = (tasks ?? [])
    .slice()
    .sort((a, b) => Number(b.created_at) - Number(a.created_at))
    .slice(0, 20)

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <section className="bg-white rounded-lg shadow p-4">
        <h2 className="text-base font-semibold mb-3">向大脑发布命令 / 询问</h2>
        <label className="text-xs text-slate-500">指令 / 询问 prompt</label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={5}
          className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded font-mono"
          placeholder="例如：分析 workspace/test.csv 并报告行数"
        />
        <div className="grid grid-cols-2 gap-2 mt-2">
          <label className="text-xs text-slate-500">
            role
            <input value={role} onChange={(e) => setRole(e.target.value)}
              className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded" />
          </label>
          <label className="text-xs text-slate-500">
            model（可选）
            <input value={model} onChange={(e) => setModel(e.target.value)}
              className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded"
              placeholder="sonnet" />
          </label>
        </div>
        <label className="text-xs text-slate-500 mt-2 block">上下文记忆（可选）</label>
        <textarea value={ctx} onChange={(e) => setCtx(e.target.value)} rows={2}
          className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded"
          placeholder="用户偏好 / 前置上下文" />
        <button onClick={() => send(false)} disabled={busy || !prompt.trim()}
          className="mt-3 px-4 py-1.5 text-sm rounded bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-40">
          {busy ? '派发中…（大脑正在执行，请稍候）' : '派发'}
        </button>

        {result && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            {result.status === 'blocked' && result.verdict ? (
              <div className="bg-amber-50 border border-amber-200 rounded p-3">
                <div className="text-amber-700 font-semibold text-sm mb-1">⛔ security-guard 拦截</div>
                <div className="text-xs text-slate-700 mb-1">{result.verdict.reason}</div>
                {result.verdict.matched_rules.length > 0 && (
                  <div className="text-xs text-slate-500 mb-1">命中规则: {result.verdict.matched_rules.join(', ')}</div>
                )}
                {result.verdict.suggestion && (
                  <div className="text-xs text-slate-600 mb-2">建议: {result.verdict.suggestion}</div>
                )}
                <button onClick={() => send(true)} disabled={busy}
                  className="px-3 py-1 text-xs rounded bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-40">
                  强制放行
                </button>
              </div>
            ) : (
              <>
                <div className="text-xs text-slate-500 mb-2">最近一次结果</div>
                <div className="flex items-center gap-2 mb-2">
                  <StatusBadge status={result.status} />
                  {result.task_id && <span className="font-mono text-xs">{result.task_id}</span>}
                </div>
                {result.output != null && (
                  <pre className="bg-slate-50 rounded p-2 text-sm whitespace-pre-wrap max-h-60 overflow-auto">
                    {String(result.output)}
                  </pre>
                )}
                {result.error != null && (
                  <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{String(result.error)}</pre>
                )}
              </>
            )}
          </div>
        )}
      </section>

      <section className="bg-white rounded-lg shadow p-4">
        <h2 className="text-base font-semibold mb-3">历史命令</h2>
        {history.length === 0 ? (
          <div className="text-slate-400 text-sm">暂无历史</div>
        ) : (
          <div className="space-y-2">
            {history.map((t) => (
              <div key={t.task_id} className="border border-slate-100 rounded p-2">
                <div className="flex items-center gap-2 mb-1 text-xs">
                  <span className="text-slate-400" title={timeAgo(t.created_at)}>{formatTime(t.created_at)}</span>
                  <StatusBadge status={t.status} />
                  <span className="text-slate-500">→ {t.worker_id || '-'}</span>
                  <button onClick={() => setPrompt(t.prompt)}
                    className="ml-auto text-sky-600 hover:underline">回填</button>
                </div>
                <pre className="text-xs text-slate-700 whitespace-pre-wrap line-clamp-2">{t.prompt}</pre>
                {t.output != null && (
                  <pre className="text-xs text-slate-500 whitespace-pre-wrap line-clamp-2 mt-1">{String(t.output)}</pre>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
