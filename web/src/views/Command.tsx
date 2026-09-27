import { useState } from 'react'
import { api } from '../api/client'
import type { ApprovalMeta, DispatchResult, TaskRecord, RecommendResponse, TranslateResponse } from '../types'
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

  // 子代理派发开关（tri-state：null=跟随全局）与 AI 推荐/解释顾问
  const [enableSubagents, setEnableSubagents] = useState<boolean | null>(null)
  const [advisorMode, setAdvisorMode] = useState<'pre_flight' | 'plan' | 'explain' | 'all'>('all')
  const [autoApply, setAutoApply] = useState(false)
  const [advice, setAdvice] = useState<RecommendResponse | null>(null)
  const [advising, setAdvising] = useState(false)

  // 结果翻译（独立上下文，思考英文/结果中文）
  const [translation, setTranslation] = useState<TranslateResponse | null>(null)
  const [translating, setTranslating] = useState(false)

  // 审批操作：修订弹窗 / 反馈信息 / 提交中标记
  const [reviseId, setReviseId] = useState<string | null>(null)
  const [revisePrompt, setRevisePrompt] = useState('')
  const [approvalMsg, setApprovalMsg] = useState('')
  const [approving, setApproving] = useState(false)

  // 历史命令从后端 /tasks 取（持久化，含发布时间/节点/结果）；5s 轮询
  const { data: tasks, refresh } = usePolling<TaskRecord[]>(() => api.tasks(), 5000)

  const send = async (force = false) => {
    if (!prompt.trim()) return
    setBusy(true)
    setResult(null)
    setTranslation(null)
    try {
      const res = await api.dispatch({
        prompt, role,
        context_memory: ctx || null,
        model: model || null,
        force,
        enable_subagents: enableSubagents,
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

  // AI 推荐/解释：调独立上下文顾问（不污染任务上下文）；auto-apply 时后端会经安全关卡派发
  const recommend = async () => {
    if (!prompt.trim()) return
    setAdvising(true)
    setAdvice(null)
    try {
      const res = await api.recommend({
        task: prompt, mode: advisorMode,
        enable_subagents: enableSubagents, auto_apply: autoApply,
      })
      setAdvice(res)
      if (res.auto_applied) refresh()
    } catch (e) {
      setAdvice({ status: 'failed', error: e instanceof Error ? e.message : String(e) })
    } finally {
      setAdvising(false)
    }
  }

  // 结果翻译：调独立上下文翻译服务（思考英文/结果中文），把结果 output 译为目标语言
  const translateResult = async (text: string) => {
    if (!text || !text.trim()) return
    setTranslating(true)
    setTranslation(null)
    try {
      const res = await api.translate({ text })
      setTranslation(res)
    } catch (e) {
      setTranslation({ status: 'failed', error: e instanceof Error ? e.message : String(e) })
    } finally {
      setTranslating(false)
    }
  }

  // 放行：修订过则重新过 security_guard；原样放行则跳过重检（人工确认）
  const approve = async (id: string) => {
    setApproving(true)
    try {
      const res = await api.approveTask(id, revisePrompt.trim() || undefined)
      if (res.approved === true) {
        setApprovalMsg(`已放行并重新派发（新任务 ${res.dispatched_task_id ?? '-'}）`)
        setReviseId(null)
        setRevisePrompt('')
      } else {
        // 失败体形状：{approved?: false, status: "failed", error: "..."}，
        // 可能无 approved 字段——仅 approved===true 视为成功；
        // 失败时待审批条目保留（不清空修订框），可修订后重试或拒绝
        const detail = res.verdict?.reason
          || (res.error != null ? String(res.error) : '')
          || `status=${res.status || 'unknown'}`
        const tip = res.approved === false
          ? '修订后的指令仍未通过安全检验'
          : '放行失败'
        setApprovalMsg(`${tip}：${detail}。请继续修订后重试，或拒绝该任务`)
      }
      refresh()
    } catch (e) {
      setApprovalMsg(`放行失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setApproving(false)
    }
  }

  const reject = async (id: string) => {
    setApproving(true)
    try {
      await api.rejectTask(id)
      setApprovalMsg('已拒绝，任务置为 failed')
      refresh()
    } catch (e) {
      setApprovalMsg(`拒绝失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setApproving(false)
    }
  }

  const history = (tasks ?? [])
    .slice()
    .sort((a, b) => Number(b.created_at) - Number(a.created_at))
    .slice(0, 20)

  // 待审批任务：安全拦截挂起，等待人工裁决（新→旧）
  const pendingApprovals = (tasks ?? [])
    .filter((t) => t.status === 'awaiting_approval')
    .sort((a, b) => Number(b.created_at) - Number(a.created_at))

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

        <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-600">
          <label className="flex items-center gap-1">
            子代理派发
            <select
              value={enableSubagents === null ? 'default' : enableSubagents ? 'on' : 'off'}
              onChange={(e) => setEnableSubagents(e.target.value === 'default' ? null : e.target.value === 'on')}
              className="px-1 py-0.5 border border-slate-300 rounded">
              <option value="default">跟随全局</option>
              <option value="on">启用</option>
              <option value="off">禁用</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            顾问模式
            <select value={advisorMode}
              onChange={(e) => setAdvisorMode(e.target.value as 'pre_flight' | 'plan' | 'explain' | 'all')}
              className="px-1 py-0.5 border border-slate-300 rounded">
              <option value="all">全部</option>
              <option value="pre_flight">派发前建议</option>
              <option value="plan">任务拆解</option>
              <option value="explain">结果解释</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={autoApply} onChange={(e) => setAutoApply(e.target.checked)} />
            自动执行推荐
          </label>
          <button onClick={recommend} disabled={advising || !prompt.trim()}
            className="px-3 py-1 rounded bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-40">
            {advising ? '顾问思考中…' : '推荐并解释'}
          </button>
        </div>
        <div className="mt-1 text-[11px] text-slate-400">顾问运行于独立上下文，不会污染本任务的会话/记忆/经验；自动执行仍经安全关卡。</div>

        {result && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            {result.status === 'awaiting_approval' && result.verdict ? (
              <div className="bg-amber-50 border border-amber-200 rounded p-3">
                <div className="text-amber-700 font-semibold text-sm mb-1">⛔ security-guard 拦截，已加入待审批</div>
                <div className="text-xs text-slate-700 mb-1">{result.verdict.reason}</div>
                {result.verdict.matched_rules.length > 0 && (
                  <div className="text-xs text-slate-500 mb-1">命中规则: {result.verdict.matched_rules.join(', ')}</div>
                )}
                {result.verdict.suggestion && (
                  <div className="text-xs text-slate-600 mb-2">建议: {result.verdict.suggestion}</div>
                )}
                {result.task_id && (
                  <div className="text-xs text-slate-500">
                    任务 <span className="font-mono">{result.task_id}</span> 已挂起，请在下方「待审批任务」中修订放行或拒绝
                  </div>
                )}
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
                {result.output != null && (
                  <div className="mt-2">
                    <button onClick={() => translateResult(String(result.output))}
                      disabled={translating}
                      className="px-3 py-1 text-xs rounded bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-40">
                      {translating ? '翻译中…' : '翻译成中文'}
                    </button>
                    {translation && (
                      <div className="mt-2">
                        <div className="text-xs text-slate-500 mb-1">
                          译文（{translation.translation?.target_lang || 'zh'} · 独立上下文，未污染任务）
                        </div>
                        {translation.status !== 'ok' || !translation.translation ? (
                          <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{translation.error || translation.status}</pre>
                        ) : translation.translation.error ? (
                          <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{translation.translation.error}</pre>
                        ) : translation.translation.skipped ? (
                          <div className="text-xs text-slate-500">
                            {translation.translation.skipped === 'already_target_lang' ? '结果已是中文，无需翻译' : '待翻译文本为空'}
                          </div>
                        ) : (
                          <pre className="bg-teal-50 rounded p-2 text-sm whitespace-pre-wrap max-h-60 overflow-auto">{translation.translation.translated}</pre>
                        )}
                      </div>
                    )}
                  </div>
                )}
                {result.error != null && (
                  <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{String(result.error)}</pre>
                )}
                {result.product_verdict?.verdict === 'block' && (
                  <div className="mt-2 bg-amber-50 border border-amber-200 rounded p-3">
                    <div className="text-amber-700 font-semibold text-sm mb-1">⚠ 产物被 security-guard 标记</div>
                    <div className="text-xs text-slate-700 mb-1">{result.product_verdict.reason}</div>
                    {result.product_verdict.matched_rules.length > 0 && (
                      <div className="text-xs text-slate-500 mb-1">命中: {result.product_verdict.matched_rules.join(', ')}</div>
                    )}
                    {result.product_verdict.suggestion && (
                      <div className="text-xs text-slate-600">建议: {result.product_verdict.suggestion}</div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {advice && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <div className="text-xs text-slate-500 mb-2">AI 推荐 / 解释（独立上下文）</div>
            {advice.status !== 'ok' || !advice.recommendation ? (
              <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{advice.error || advice.status}</pre>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`text-xs px-2 py-0.5 rounded ${advice.recommendation.should_dispatch ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}>
                    {advice.recommendation.should_dispatch ? '建议派发子代理' : '单 agent 即可完成'}
                  </span>
                  {advice.recommendation.roles.length > 0 && (
                    <span className="text-xs text-slate-500">角色: {advice.recommendation.roles.join(', ')}</span>
                  )}
                  {advice.auto_applied && <span className="text-xs text-violet-600">已自动执行</span>}
                </div>
                {advice.recommendation.explanation && (
                  <div className="text-sm text-slate-700 whitespace-pre-wrap">{advice.recommendation.explanation}</div>
                )}
                {advice.recommendation.plan.length > 0 && (
                  <ol className="list-decimal list-inside text-xs text-slate-600 space-y-0.5">
                    {advice.recommendation.plan.map((s, i) => (
                      <li key={i}>{s.action}{s.depends_on && s.depends_on.length ? ` （依赖 ${s.depends_on.join(', ')}）` : ''}</li>
                    ))}
                  </ol>
                )}
                {advice.recommendation.risks.length > 0 && (
                  <div className="text-xs text-amber-700">风险: {advice.recommendation.risks.join('；')}</div>
                )}
                {(advice.dispatched ?? []).map((d, i) => (
                  <div key={i} className="text-xs text-slate-500">
                    派发[{d.role}] {d.status}{d.task_id ? ` · ${d.task_id}` : ''}{d.reason ? ` · ${d.reason}` : ''}
                  </div>
                ))}
              </div>
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
                  {t.verdict?.verdict === 'block' && (
                    <span className="text-amber-600 text-xs" title={t.verdict.reason}>⚠ 产物拦截</span>
                  )}
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

      {pendingApprovals.length > 0 && (
        <section className="bg-white rounded-lg shadow p-4 lg:col-span-2">
          <h2 className="text-base font-semibold mb-3">待审批任务（安全拦截挂起）</h2>
          {approvalMsg && <div className="mb-2 text-xs text-slate-600">{approvalMsg}</div>}
          <div className="space-y-2">
            {pendingApprovals.map((t) => {
              const appr = (t.meta?.approval ?? {}) as ApprovalMeta
              const verdict = appr.verdict ?? t.verdict
              return (
                <div key={t.task_id} className="border border-amber-200 rounded p-2">
                  <div className="flex items-center gap-2 mb-1 text-xs">
                    <span className="text-slate-400" title={timeAgo(t.created_at)}>{formatTime(t.created_at)}</span>
                    <StatusBadge status={t.status} />
                    <span className="font-mono text-slate-500">{t.task_id}</span>
                    <span className="text-slate-500">role: {t.role}</span>
                    <div className="ml-auto flex gap-2">
                      <button
                        onClick={() => { setReviseId(t.task_id); setRevisePrompt(t.prompt); setApprovalMsg('') }}
                        className="px-3 py-1 text-xs rounded bg-sky-600 text-white hover:bg-sky-700">修订后放行</button>
                      <button onClick={() => reject(t.task_id)} disabled={approving}
                        className="px-3 py-1 text-xs rounded bg-rose-600 text-white hover:bg-rose-700 disabled:opacity-40">拒绝</button>
                    </div>
                  </div>
                  <pre className="text-xs text-slate-700 whitespace-pre-wrap line-clamp-2">{t.prompt}</pre>
                  {verdict && (
                    <div className="text-xs text-slate-500 mt-1">
                      拦截原因: {verdict.reason}
                      {verdict.matched_rules.length > 0 && `｜命中: ${verdict.matched_rules.join(', ')}`}
                    </div>
                  )}
                  {reviseId === t.task_id && (
                    <div className="mt-2">
                      <textarea value={revisePrompt} onChange={(e) => setRevisePrompt(e.target.value)} rows={3}
                        className="w-full px-2 py-1 text-sm border border-slate-300 rounded font-mono"
                        placeholder="修订后的指令（保存后重新过安全检验）" />
                      <div className="flex gap-2 mt-1">
                        <button onClick={() => approve(t.task_id)} disabled={approving}
                          className="px-3 py-1 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-40">放行</button>
                        <button onClick={() => setReviseId(null)}
                          className="px-3 py-1 text-xs rounded bg-slate-200 text-slate-600 hover:bg-slate-300">取消</button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}
