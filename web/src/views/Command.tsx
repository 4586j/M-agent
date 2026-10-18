import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { getActiveConnection, useActiveConnection } from '../api/connections'
import { migrateSessionSelection, readSessionSelection, saveSessionSelection, sessionSelectionKey } from '../api/brainSessions'
import type { ApprovalMeta, DispatchBody, DispatchAsyncResponse, DispatchResult, ExperienceRecord, ProviderList, TaskRecord, RecommendResponse, TranslateResponse, WorkerInfo, BrainSession } from '../types'
import { TERMINAL_STATUSES } from '../types'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'
import { fmtUsd, formatTime, timeAgo } from '../utils'
import StatusBadge from '../components/StatusBadge'

const QUICK_PROMPTS = [
  '分析工作区文件结构并报告概览',
  '检查核心模块并生成自动化测试用例',
  '诊断集群运行状态与高负载节点',
  '审计安全规则并梳理潜在阻断风险',
]

export default function Command() {
  const connection = useActiveConnection()
  const sessionKey = sessionSelectionKey(connection)
  return <BrainCommand key={sessionKey} sessionKey={sessionKey} />
}

function BrainCommand({ sessionKey }: { sessionKey: string }) {
  const [prompt, setPrompt] = useState('')
  const [role, setRole] = useState('executor')
  const [model, setModel] = useState('')
  const [provider, setProvider] = useState('')
  const [ctx, setCtx] = useState('')
  const [result, setResult] = useState<DispatchResult | null>(null)
  const [busy, setBusy] = useState(false)

  // 定点派发：从 NodeManage「派发到此节点」跳转带 workerId，或本页下拉选择
  const location = useLocation()
  const [workerId, setWorkerId] = useState<string>(() => {
    const target = location.state as { workerId?: string; brainKey?: string } | null
    return target?.brainKey === sessionKey ? target.workerId ?? '' : ''
  })
  const { data: workers } = usePolling<WorkerInfo[]>(() => api.workers(), POLL.workers)

  // 从 NodeManage 跳转带 workerId 时，工人列表异步返回后回填其真实 role（role 输入已随定点禁用）
  useEffect(() => {
    const w = workerId ? (workers ?? []).find((x) => x.worker_id === workerId) : undefined
    if (w) setRole(w.role)
  }, [workerId, workers])

  // 运行时 Provider 名列表（brain providers.json；省略=用当前默认）
  const { data: providerList } = usePolling<ProviderList>(() => api.providers(), POLL.providers)

  // 二期：同步阻塞 / 异步+SSE 两种派发模式（异步适合 web-expert/back-expert 长流程）
  const [mode, setMode] = useState<'sync' | 'async'>('sync')
  const [asyncRes, setAsyncRes] = useState<DispatchAsyncResponse | null>(null)
  // 二期：引用历史任务上下文（context_task_ids 随派发并入 context_memory）
  const [ctxTaskIds, setCtxTaskIds] = useState<string[]>([])
  // 二期：检索相似经验一键填充
  const [expResults, setExpResults] = useState<ExperienceRecord[] | null>(null)
  const [expSearching, setExpSearching] = useState(false)

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

  // 持久会话（连续对话）：历史轮次自动注入上下文；id 存 localStorage 跨刷新续聊
  const [sessionId, setSessionId] = useState<string>(
    () => readSessionSelection(sessionKey))
  const sessionChoice = useRef(sessionId)
  const pendingSession = useRef('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const { data: sessionList, refresh: refreshSessions } = usePolling<{ sessions: BrainSession[] }>(
    () => api.sessions(), POLL.services)

  const pickSession = (id: string) => {
    sessionChoice.current = id
    setSessionId(id)
    saveSessionSelection(sessionKey, id)
  }

  useEffect(() => {
    if (!sessionList) return
    const ids = sessionList.sessions.map(s => s.session_id)
    const selected = sessionChoice.current
    if (pendingSession.current && selected === pendingSession.current && !ids.includes(selected)) return
    pendingSession.current = ''
    const valid = ids.includes(selected) ? selected : migrateSessionSelection(sessionKey, ids)
    if (selected && !valid) saveSessionSelection(sessionKey, '')
    sessionChoice.current = valid
    setSessionId(valid)
  }, [sessionList, sessionKey])

  const newSession = async () => {
    try {
      const r = await api.createSession()
      if (!mounted.current || sessionSelectionKey(getActiveConnection()) !== sessionKey) return
      pendingSession.current = r.session_id
      pickSession(r.session_id)
      refreshSessions()
    } catch { /* 创建失败保持当前会话不变 */ }
  }

  // 历史命令从后端 /tasks 取（持久化，含发布时间/节点/结果）；5s 轮询
  const { data: tasks, refresh } = usePolling<TaskRecord[]>(() => api.tasks(), POLL.tasks)
  const dispatchBody = useMemo<DispatchBody>(() => ({
    prompt, role, context_memory: ctx || null, model: model || null, provider: provider || null,
    enable_subagents: enableSubagents, context_task_ids: ctxTaskIds,
    worker_id: workerId || null, session_id: sessionId || null,
  }), [prompt,role,ctx,model,provider,enableSubagents,ctxTaskIds,workerId,sessionId])

  const send = async (force = false) => {
    if (sessionSelectionKey(getActiveConnection()) !== sessionKey) return
    if (!prompt.trim()) return
    setBusy(true)
    setResult(null)
    setTranslation(null)
    setAsyncRes(null)
    try {
      const body = {...dispatchBody, force}
      if (mode === 'async') {
        const res = await api.dispatchAsync(body)
        setAsyncRes(res)
        if (res.status === 'accepted') refresh()
      } else {
        const res = await api.dispatch(body)
        setResult(res)
        if (res.status !== 'blocked') refresh()
      }
    } catch (e) {
      setResult({
        task_id: '', status: 'failed', output: null, artifacts: [],
        error: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setBusy(false)
    }
  }

  // 二期：检索相似经验（混合检索 top-3 成功经验），一键附加为上下文记忆
  const searchExperiences = async () => {
    if (!prompt.trim()) return
    setExpSearching(true)
    try {
      setExpResults(await api.searchExperiences({ query: prompt, successful_only: true, limit: 3 }))
    } catch {
      setExpResults([])
    } finally {
      setExpSearching(false)
    }
  }

  const attachExperience = (r: ExperienceRecord) => {
    const block = `[历史经验参考] ${r.exp_id}（${r.success ? '成功' : '失败'}）\n任务: ${r.prompt}\n产出: ${r.output_summary}`
    setCtx((prev) => (prev ? `${prev}\n\n${block}` : block))
    setExpResults(null)
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

  // 派生列表仅随 tasks 变化重算
  const history = useMemo(
    () => (tasks ?? [])
      .slice()
      .sort((a, b) => Number(b.created_at) - Number(a.created_at))
      .slice(0, 20),
    [tasks],
  )

  // 待审批任务：安全拦截挂起，等待人工裁决
  const pendingApprovals = useMemo(
    () => (tasks ?? [])
      .filter((t) => t.status === 'awaiting_approval')
      .sort((a, b) => Number(b.created_at) - Number(a.created_at)),
    [tasks],
  )

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* 派发卡片 */}
      <section className="spotlight-card stagger-item bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
            <h2 className="text-sm font-semibold text-zinc-100 tracking-tight">向大脑发布命令 / 询问</h2>
          </div>
          <span className="text-[11px] text-zinc-500 font-mono bg-zinc-800/60 border border-white/[0.06] px-2 py-0.5 rounded">
            ⌘/Ctrl+Enter 快捷派发
          </span>
        </div>

        {/* 快捷指令芯片 */}
        <div className="flex flex-wrap gap-1.5 mb-2.5">
          {QUICK_PROMPTS.map((qp) => (
            <button
              key={qp}
              type="button"
              onClick={() => setPrompt(qp)}
              className="text-[11px] text-zinc-400 hover:text-emerald-300 bg-zinc-950/60 hover:bg-zinc-800/80 border border-white/[0.06] hover:border-emerald-500/30 rounded-lg px-2.5 py-1 transition duration-150"
            >
              {qp}
            </button>
          ))}
        </div>

        <div className={`rounded-xl transition duration-300 ${busy ? 'animate-pulse-glow border border-emerald-500/50 shadow-[0_0_30px_-5px_rgba(16,185,129,0.35)]' : 'focus-within:border-emerald-500/50 focus-within:ring-2 focus-within:ring-emerald-500/20 focus-within:shadow-[0_0_30px_-5px_rgba(16,185,129,0.25)] border border-transparent'}`}>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !busy && prompt.trim()) {
                e.preventDefault()
                void send(false)
              }
            }}
            rows={5}
            className="w-full px-3.5 py-3 text-sm border border-white/[0.08] rounded-xl font-mono focus:outline-none transition bg-zinc-950/80 text-zinc-100 placeholder:text-zinc-600 resize-y"
            placeholder="例如：分析 workspace/test.csv 并报告行数"
          />
        </div>

        {/* 核心参数配置矩阵 */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5 mt-3">
          <label className="text-[11px] text-zinc-400 font-medium">
            会话（连续对话）
            <div className="mt-1 flex gap-1">
              <select
                value={sessionId}
                onChange={(e) => pickSession(e.target.value)}
                className="flex-1 min-w-0 px-2 py-1 text-xs border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 focus:outline-none focus:border-emerald-500/40"
                title="选中会话后派发自动携带历史轮次；新会话请点右侧 ＋"
              >
                <option value="">单发（不记忆）</option>
                {(sessionList?.sessions ?? []).map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {(s.title || s.session_id).slice(0, 24)}（{s.turn_count}轮）
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={newSession}
                title="创建新会话"
                className="px-2 py-1 text-xs rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 border border-emerald-500/30 transition"
              >
                ＋
              </button>
            </div>
          </label>

          <label className="text-[11px] text-zinc-400 font-medium">
            role
            <input
              value={role}
              disabled={!!workerId}
              onChange={(e) => setRole(e.target.value)}
              className="mt-1 w-full px-2 py-1 text-xs font-mono border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 disabled:bg-zinc-900/60 disabled:text-zinc-500 focus:outline-none focus:border-emerald-500/40"
              title="定点后由节点真实 role 决定，自动回填"
            />
          </label>

          <label className="text-[11px] text-zinc-400 font-medium">
            model（可选）
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="mt-1 w-full px-2 py-1 text-xs font-mono border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
              placeholder="sonnet"
            />
          </label>

          <label className="text-[11px] text-zinc-400 font-medium">
            provider（可选）
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="mt-1 w-full px-2 py-1 text-xs border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 focus:outline-none focus:border-emerald-500/40"
              title={`空 = 用 brain 当前默认${providerList?.default ? `（${providerList.default}）` : ''}；只传 provider 名，密钥在 brain 侧解析`}
            >
              <option value="">默认{providerList?.default ? `（${providerList.default}）` : ''}</option>
              {Object.keys(providerList?.providers ?? {}).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>

          <label className="text-[11px] text-zinc-400 font-medium col-span-2 md:col-span-4">
            目标节点
            <select
              value={workerId}
              onChange={(e) => {
                const id = e.target.value
                setWorkerId(id)
                if (id) {
                  const w = (workers ?? []).find((x) => x.worker_id === id)
                  if (w) setRole(w.role)
                }
              }}
              className="mt-1 w-full px-2.5 py-1.5 text-xs font-mono border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 focus:outline-none focus:border-emerald-500/40"
              title="空 = 自动按 role 轮询；选中即定点派发，role 跟随节点"
            >
              <option value="">自动（按 role 轮询集群负载调度）</option>
              {(workers ?? []).map((w) => (
                <option key={w.worker_id} value={w.worker_id}>
                  {w.worker_id} · {w.role} · {w.host}:{w.port} (load {w.load})
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* 上下文记忆区 */}
        <div className="mt-3">
          <div className="flex items-center justify-between mb-1">
            <label className="text-[11px] text-zinc-400 font-medium">上下文记忆（可选）</label>
            <button
              type="button"
              onClick={searchExperiences}
              disabled={expSearching || !prompt.trim()}
              className="text-xs text-emerald-400 hover:text-emerald-300 hover:underline font-medium disabled:opacity-40"
            >
              {expSearching ? '检索相似经验…' : '↸ 检索相似经验并附加'}
            </button>
          </div>
          <textarea
            value={ctx}
            onChange={(e) => setCtx(e.target.value)}
            rows={2}
            className="w-full px-3 py-1.5 text-xs font-mono border border-white/[0.08] rounded-lg bg-zinc-950/80 text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
            placeholder="用户偏好 / 前置上下文"
          />
        </div>

        {expResults && (
          <div className="mt-2 space-y-1.5 bg-zinc-950/60 border border-white/[0.06] rounded-xl p-2.5">
            {expResults.length === 0 && (
              <div className="text-xs text-zinc-500">没有相似的成功经验</div>
            )}
            {expResults.map((r) => (
              <div key={r.exp_id} className="flex items-center gap-2 bg-zinc-900/80 border border-white/[0.06] rounded-lg px-2.5 py-1.5">
                <StatusBadge status={r.success ? 'done' : 'failed'} />
                <span className="text-xs text-zinc-300 truncate flex-1 font-mono" title={r.output_summary}>
                  {r.prompt.length > 50 ? r.prompt.slice(0, 50) + '…' : r.prompt}
                </span>
                <button
                  type="button"
                  onClick={() => attachExperience(r)}
                  className="px-2.5 py-0.5 text-xs rounded bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 border border-emerald-500/30 shrink-0 font-medium"
                >
                  附加
                </button>
              </div>
            ))}
            <button type="button" onClick={() => setExpResults(null)} className="text-[11px] text-zinc-500 hover:text-zinc-300 hover:underline">
              收起
            </button>
          </div>
        )}

        {/* 引用历史任务上下文 */}
        <div className="mt-3">
          <label className="text-[11px] text-zinc-400 font-medium">引用历史任务上下文（续做任务时勾选，笔记将并入本次派发）</label>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {(tasks ?? []).slice(0, 8).map((t) => {
              const on = ctxTaskIds.includes(t.task_id)
              return (
                <button
                  key={t.task_id}
                  type="button"
                  onClick={() => setCtxTaskIds((prev) => on ? prev.filter((x) => x !== t.task_id) : [...prev, t.task_id])}
                  className={`px-2.5 py-1 text-xs rounded-lg font-mono transition duration-150 ${
                    on
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 shadow-sm'
                      : 'bg-zinc-950/70 text-zinc-400 border border-white/[0.06] hover:bg-zinc-800 hover:text-zinc-200'
                  }`}
                >
                  {t.task_id}
                </button>
              )
            })}
            {(tasks ?? []).length === 0 && <span className="text-xs text-zinc-500">暂无历史任务</span>}
          </div>
        </div>
        {/* 派发操作条 */}
        <div className="mt-4 flex items-center gap-3">
          <div className="inline-flex p-0.5 bg-zinc-950 rounded-lg border border-white/[0.08] text-xs" role="group" aria-label="派发模式">
            {(['sync', 'async'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`px-3 py-1 rounded-md transition font-medium ${
                  mode === m
                    ? 'bg-zinc-800 text-emerald-400 shadow-sm font-semibold'
                    : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {m === 'sync' ? '同步阻塞' : '异步+SSE'}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => send(false)}
            disabled={busy || !prompt.trim()}
            className={`btn-press px-6 py-1.5 text-xs rounded-xl font-semibold transition disabled:opacity-40 ${
              mode === 'async'
                ? 'bg-emerald-500 hover:bg-emerald-400 text-zinc-950 shadow-[0_0_15px_rgba(16,185,129,0.3)]'
                : 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-[0_0_15px_rgba(99,102,241,0.3)]'
            }`}
          >
            {busy ? '派发中…' : mode === 'async' ? '异步派发' : '派发'}
          </button>

          <span className="text-[11px] text-zinc-500 font-mono">
            {mode === 'async' ? '立即返回 task_id，下方实时跟踪' : '等待大脑执行完成返回'}
          </span>
        </div>

        {/* 顾问与增强控制 */}
        <div className="mt-4 pt-3 border-t border-white/[0.06] flex flex-wrap items-center gap-3 text-xs text-zinc-300">
          <label className="flex items-center gap-1.5">
            <span className="text-zinc-400">子代理</span>
            <select
              value={enableSubagents === null ? 'default' : enableSubagents ? 'on' : 'off'}
              onChange={(e) => setEnableSubagents(e.target.value === 'default' ? null : e.target.value === 'on')}
              className="px-2 py-0.5 border border-white/[0.08] rounded-md bg-zinc-950 text-zinc-200"
            >
              <option value="default">跟随全局</option>
              <option value="on">启用</option>
              <option value="off">禁用</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5">
            <span className="text-zinc-400">顾问模式</span>
            <select
              value={advisorMode}
              onChange={(e) => setAdvisorMode(e.target.value as 'pre_flight' | 'plan' | 'explain' | 'all')}
              className="px-2 py-0.5 border border-white/[0.08] rounded-md bg-zinc-950 text-zinc-200"
            >
              <option value="all">全部</option>
              <option value="pre_flight">派发前建议</option>
              <option value="plan">任务拆解</option>
              <option value="explain">结果解释</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoApply}
              onChange={(e) => setAutoApply(e.target.checked)}
              className="accent-emerald-500"
            />
            <span className="text-zinc-300">自动执行推荐</span>
          </label>

          <button
            type="button"
            onClick={recommend}
            disabled={advising || !prompt.trim()}
            className="px-3 py-1 rounded-lg bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 border border-indigo-500/30 transition font-medium disabled:opacity-40"
          >
            {advising ? '顾问思考中…' : '推荐并解释'}
          </button>
        </div>
        <div className="mt-1.5 text-[11px] text-zinc-500">
          顾问运行于独立上下文，不会污染本任务的会话/记忆/经验；自动执行仍经安全关卡。
        </div>

        {/* 派发结果展示 */}
        {result && (
          <div className="mt-4 border-t border-white/[0.07] pt-4">
            {result.status === 'awaiting_approval' && result.verdict ? (
              <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-3.5 backdrop-blur-md">
                <div className="text-amber-400 font-semibold text-sm mb-1 flex items-center gap-1.5">
                  <span>⛔</span> security-guard 拦截，已加入待审批
                </div>
                <div className="text-xs text-zinc-300 mb-1">{result.verdict.reason}</div>
                {result.verdict.matched_rules.length > 0 && (
                  <div className="text-xs text-zinc-400 mb-1 font-mono">
                    命中规则: {result.verdict.matched_rules.join(', ')}
                  </div>
                )}
                {result.verdict.suggestion && (
                  <div className="text-xs text-amber-300/80 mb-2">建议: {result.verdict.suggestion}</div>
                )}
                {result.task_id && (
                  <div className="text-xs text-zinc-400 font-mono">
                    任务 <span className="text-amber-400">{result.task_id}</span> 已挂起，请在下方「待审批任务」中修订放行或拒绝
                  </div>
                )}
              </div>
            ) : (
              <>
                <div className="text-xs text-zinc-400 mb-2 font-medium">最近一次结果</div>
                <div className="flex items-center gap-2 mb-2">
                  <StatusBadge status={result.status} />
                  {result.task_id && <span className="font-mono text-xs text-zinc-400">{result.task_id}</span>}
                </div>
                {result.output != null && (
                  <pre className="bg-[#050507] border border-white/[0.08] text-zinc-200 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap max-h-60 overflow-auto">
                    {String(result.output)}
                  </pre>
                )}
                {result.output != null && (
                  <div className="mt-2.5">
                    <button
                      type="button"
                      onClick={() => translateResult(String(result.output))}
                      disabled={translating}
                      className="px-3 py-1 text-xs rounded-lg bg-teal-500/20 text-teal-300 hover:bg-teal-500/30 border border-teal-500/30 transition disabled:opacity-40"
                    >
                      {translating ? '翻译中…' : '翻译成中文'}
                    </button>
                    {translation && (
                      <div className="mt-2 bg-zinc-950/80 border border-white/[0.07] rounded-xl p-3">
                        <div className="text-xs text-zinc-400 mb-1 font-mono">
                          译文（{translation.translation?.target_lang || 'zh'} · 独立上下文，未污染任务）
                        </div>
                        {translation.status !== 'ok' || !translation.translation ? (
                          <pre className="text-rose-400 text-xs font-mono whitespace-pre-wrap">{translation.error || translation.status}</pre>
                        ) : translation.translation.error ? (
                          <pre className="text-rose-400 text-xs font-mono whitespace-pre-wrap">{translation.translation.error}</pre>
                        ) : translation.translation.skipped ? (
                          <div className="text-xs text-zinc-400">
                            {translation.translation.skipped === 'already_target_lang' ? '结果已是中文，无需翻译' : '待翻译文本为空'}
                          </div>
                        ) : (
                          <pre className="text-emerald-300 text-xs font-mono whitespace-pre-wrap max-h-60 overflow-auto">{translation.translation.translated}</pre>
                        )}
                      </div>
                    )}
                  </div>
                )}
                {result.error != null && (
                  <pre className="mt-2 bg-rose-500/10 border border-rose-500/20 text-rose-300 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap">
                    {String(result.error)}
                  </pre>
                )}
                {result.product_verdict?.verdict === 'block' && (
                  <div className="mt-2 bg-amber-500/10 border border-amber-500/30 rounded-xl p-3">
                    <div className="text-amber-400 font-semibold text-xs mb-1">⚠ 产物被 security-guard 标记</div>
                    <div className="text-xs text-zinc-300 mb-1">{result.product_verdict.reason}</div>
                    {result.product_verdict.matched_rules.length > 0 && (
                      <div className="text-xs text-zinc-400 mb-1 font-mono">命中: {result.product_verdict.matched_rules.join(', ')}</div>
                    )}
                    {result.product_verdict.suggestion && (
                      <div className="text-xs text-amber-300/80">建议: {result.product_verdict.suggestion}</div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {asyncRes && (
          <div className="mt-4 border-t border-white/[0.07] pt-3">
            {asyncRes.status !== 'accepted' ? (
              <pre className="bg-rose-500/10 border border-rose-500/20 text-rose-300 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap">
                异步派发失败：{asyncRes.error || asyncRes.status}
              </pre>
            ) : (
              <AsyncTaskCard taskId={asyncRes.task_id} onFinished={refresh} />
            )}
          </div>
        )}

        {advice && (
          <div className="mt-4 border-t border-white/[0.07] pt-3">
            <div className="text-xs text-zinc-400 mb-2 font-medium">AI 推荐 / 解释（独立上下文）</div>
            {advice.status !== 'ok' || !advice.recommendation ? (
              <pre className="bg-rose-500/10 border border-rose-500/20 text-rose-300 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap">{advice.error || advice.status}</pre>
            ) : (
              <div className="space-y-2 bg-zinc-950/80 border border-white/[0.07] rounded-xl p-3.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`text-xs px-2 py-0.5 rounded font-mono ${advice.recommendation.should_dispatch ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-zinc-800 text-zinc-400'}`}>
                    {advice.recommendation.should_dispatch ? '建议派发子代理' : '单 agent 即可完成'}
                  </span>
                  {advice.recommendation.roles.length > 0 && (
                    <span className="text-xs text-zinc-400 font-mono">角色: {advice.recommendation.roles.join(', ')}</span>
                  )}
                  {advice.auto_applied && <span className="text-xs text-indigo-400 font-medium">已自动执行</span>}
                </div>
                {advice.recommendation.explanation && (
                  <div className="text-xs text-zinc-300 whitespace-pre-wrap">{advice.recommendation.explanation}</div>
                )}
                {advice.recommendation.plan.length > 0 && (
                  <ol className="list-decimal list-inside text-xs text-zinc-400 space-y-1 font-mono">
                    {advice.recommendation.plan.map((s, i) => (
                      <li key={i}>{s.action}{s.depends_on && s.depends_on.length ? ` （依赖 ${s.depends_on.join(', ')}）` : ''}</li>
                    ))}
                  </ol>
                )}
                {advice.recommendation.risks.length > 0 && (
                  <div className="text-xs text-amber-400">风险: {advice.recommendation.risks.join('；')}</div>
                )}
                {(advice.dispatched ?? []).map((d, i) => (
                  <div key={i} className="text-xs text-zinc-400 font-mono">
                    派发[{d.role}] {d.status}{d.task_id ? ` · ${d.task_id}` : ''}{d.reason ? ` · ${d.reason}` : ''}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* 历史命令列表 */}
      <section
        style={{ '--stagger': 1 } as React.CSSProperties}
        className="spotlight-card stagger-item bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] flex flex-col"
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-zinc-100 tracking-tight">历史命令</h2>
          <span className="text-xs text-zinc-500 font-mono tabular-nums">{history.length} 条</span>
        </div>
        {history.length === 0 ? (
          <div className="text-zinc-500 text-sm py-12 text-center">暂无历史</div>
        ) : (
          <div className="space-y-2 overflow-y-auto max-h-[calc(100vh-280px)] pr-1">
            {history.map((t) => (
              <div key={t.task_id} className="bg-zinc-950/60 border border-white/[0.06] hover:border-white/[0.12] rounded-xl p-3 transition duration-150 group">
                <div className="flex items-center gap-2 mb-1.5 text-xs">
                  <span className="text-zinc-500 font-mono tabular-nums" title={timeAgo(t.created_at)}>{formatTime(t.created_at)}</span>
                  <StatusBadge status={t.status} />
                  {t.verdict?.verdict === 'block' && (
                    <span className="text-amber-400 text-xs" title={t.verdict.reason}>⚠ 产物拦截</span>
                  )}
                  <span className="text-zinc-500 font-mono">→ {t.worker_id || '-'}</span>
                  <button
                    type="button"
                    onClick={() => setPrompt(t.prompt)}
                    className="ml-auto text-emerald-400 hover:text-emerald-300 hover:underline font-medium"
                  >
                    回填
                  </button>
                </div>
                <pre className="text-xs text-zinc-200 whitespace-pre-wrap line-clamp-2 font-mono group-hover:text-zinc-100 transition">
                  {t.prompt}
                </pre>
                {t.output != null && (
                  <pre className="text-xs text-zinc-500 whitespace-pre-wrap line-clamp-2 mt-1.5 font-mono border-t border-white/[0.04] pt-1">
                    {String(t.output)}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 待审批任务卡片 */}
      {pendingApprovals.length > 0 && (
        <section
          style={{ '--stagger': 2 } as React.CSSProperties}
          className="spotlight-card stagger-item bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-amber-500/20 p-5 shadow-[inset_0_1px_0_0_rgba(245,158,11,0.05)] lg:col-span-2"
        >
          <div className="flex items-center gap-2 mb-3">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse" />
            <h2 className="text-sm font-semibold text-amber-300 tracking-tight">待审批任务（安全拦截挂起）</h2>
            <span className="text-xs font-mono text-amber-400/80 bg-amber-500/10 px-2 py-0.5 rounded-full border border-amber-500/20">
              {pendingApprovals.length} 待处理
            </span>
          </div>

          {approvalMsg && (
            <div className="mb-3 text-xs text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded-xl px-3 py-2">
              {approvalMsg}
            </div>
          )}

          <div className="space-y-2.5">
            {pendingApprovals.map((t) => {
              const appr = (t.meta?.approval ?? {}) as ApprovalMeta
              const verdict = appr.verdict ?? t.verdict
              return (
                <div key={t.task_id} className="bg-zinc-950/80 border border-amber-500/20 rounded-xl p-3.5">
                  <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
                    <span className="text-zinc-500 font-mono tabular-nums" title={timeAgo(t.created_at)}>{formatTime(t.created_at)}</span>
                    <StatusBadge status={t.status} />
                    <span className="font-mono text-zinc-400">{t.task_id}</span>
                    <span className="text-zinc-400 font-mono">role: {t.role}</span>
                    <div className="ml-auto flex gap-2">
                      <button
                        type="button"
                        onClick={() => { setReviseId(t.task_id); setRevisePrompt(t.prompt); setApprovalMsg('') }}
                        className="px-3 py-1 text-xs rounded-lg bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 border border-indigo-500/30 transition font-medium"
                      >
                        修订后放行
                      </button>
                      <button
                        type="button"
                        onClick={() => reject(t.task_id)}
                        disabled={approving}
                        className="px-3 py-1 text-xs rounded-lg bg-rose-500/20 text-rose-300 hover:bg-rose-500/30 border border-rose-500/30 transition font-medium disabled:opacity-40"
                      >
                        拒绝
                      </button>
                    </div>
                  </div>
                  <pre className="text-xs text-zinc-200 whitespace-pre-wrap line-clamp-2 font-mono bg-zinc-900/60 p-2 rounded-lg border border-white/[0.05]">
                    {t.prompt}
                  </pre>
                  {verdict && (
                    <div className="text-xs text-amber-400/90 mt-2">
                      拦截原因: {verdict.reason}
                      {verdict.matched_rules.length > 0 && ` ｜ 命中: ${verdict.matched_rules.join(', ')}`}
                    </div>
                  )}
                  {reviseId === t.task_id && (
                    <div className="mt-3 bg-zinc-900/90 border border-white/[0.08] rounded-xl p-3">
                      <textarea
                        value={revisePrompt}
                        onChange={(e) => setRevisePrompt(e.target.value)}
                        rows={3}
                        className="w-full px-3 py-2 text-xs border border-white/[0.1] rounded-lg font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
                        placeholder="修订后的指令（保存后重新过安全检验）"
                      />
                      <div className="flex gap-2 mt-2">
                        <button
                          type="button"
                          onClick={() => approve(t.task_id)}
                          disabled={approving}
                          className="px-4 py-1 text-xs rounded-lg bg-emerald-500 text-zinc-950 font-semibold hover:bg-emerald-400 transition disabled:opacity-40"
                        >
                          放行
                        </button>
                        <button
                          type="button"
                          onClick={() => setReviseId(null)}
                          className="px-3 py-1 text-xs rounded-lg bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition"
                        >
                          取消
                        </button>
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

/** 异步派发的实时跟踪卡：2s 轮询任务状态，终态展示结果与用量 */
function AsyncTaskCard({ taskId, onFinished }: { taskId: string; onFinished?: () => void }) {
  const [task, setTask] = useState<TaskRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  const finishedRef = useRef(false)
  const terminal = task ? [...TERMINAL_STATUSES, 'awaiting_approval'].includes(task.status) : false

  useEffect(() => {
    let alive = true
    let timer: number | null = null
    const tick = async () => {
      try {
        const t = await api.task(taskId)
        if (!alive) return
        setTask(t)
        if (TERMINAL_STATUSES.includes(t.status) && !finishedRef.current) {
          finishedRef.current = true
          onFinished?.()
        }
        if (t.status !== 'done') timer = window.setTimeout(tick, 2000)
      } catch (e) {
        if (!alive) return
        setError(e instanceof Error ? e.message : String(e))
        timer = window.setTimeout(tick, 5000)
      }
    }
    tick()
    return () => {
      alive = false
      if (timer !== null) clearTimeout(timer)
    }
  }, [taskId])

  return (
    <div className="bg-zinc-950/80 border border-white/[0.07] rounded-xl p-3.5">
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="text-xs text-zinc-400 font-medium">异步任务</span>
        {task ? <StatusBadge status={task.status} /> : <span className="text-xs text-zinc-500">查询中…</span>}
        <span className="font-mono text-xs text-zinc-400">{taskId}</span>
        {task?.meta?.cost_usd != null && (
          <span className="text-xs text-emerald-400 font-mono tabular-nums">{fmtUsd(task.meta.cost_usd)}</span>
        )}
      </div>
      {error && <div className="text-xs text-rose-400 mb-2">{error}</div>}
      {task?.output != null && (
        <pre className="bg-[#050507] border border-white/[0.06] rounded-lg p-2.5 text-xs font-mono text-zinc-200 whitespace-pre-wrap max-h-60 overflow-auto">
          {String(task.output)}
        </pre>
      )}
      {task?.error != null && (
        <pre className="bg-rose-500/10 border border-rose-500/20 text-rose-300 rounded-lg p-2.5 text-xs font-mono whitespace-pre-wrap">
          {String(task.error)}
        </pre>
      )}
      {task?.status === 'awaiting_approval' && (
        <div className="text-xs text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded-lg p-2">
          ⏸ 任务被安全拦截挂起，请到「总览待办中心」或任务页处理
        </div>
      )}
      {!terminal && !error && (
        <div className="text-[11px] text-zinc-500 font-mono mt-1">实时跟踪中（2s 轮询）…</div>
      )}
    </div>
  )
}
