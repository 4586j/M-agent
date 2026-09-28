import { useMemo, useState } from 'react'
import type { ScheduleRecord } from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'
import { formatTime, timeAgo } from '../utils'
import { isValidCron, nextRuns } from '../cron'

const CRON_TEMPLATES: { label: string; expr: string }[] = [
  { label: '每天 09:00', expr: '0 9 * * *' },
  { label: '工作日 09:00', expr: '0 9 * * 1-5' },
  { label: '每小时', expr: '0 * * * *' },
  { label: '每周一 18:00', expr: '0 18 * * 1' },
  { label: '每 15 分钟', expr: '*/15 * * * *' },
]

/** 调度视图：cron 定时任务管理（列表 + 创建预览 + 启停/触发/删除） */
export default function Schedules() {
  const { data: schedules, error, refresh } = usePolling(() => api.schedules(), POLL.services)
  const [creating, setCreating] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const act = async (id: string, fn: () => Promise<unknown>, label: string) => {
    setBusyId(id)
    setMsg(null)
    try {
      await fn()
      setMsg(`${label} 完成`)
      refresh()
    } catch (e) {
      setMsg(`${label} 失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">
          定时任务按 cron 表达式自动派发（经安全关卡①检验，与其他派发同链路）
        </p>
        <button onClick={() => setCreating(!creating)}
          className="px-3.5 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold transition">
          {creating ? '收起表单' : '+ 新建调度'}
        </button>
      </div>

      {msg && <div className="text-xs text-mint-700 bg-mint-50 border border-mint-200 rounded-xl px-3 py-2">{msg}</div>}
      {error && (
        <div className="text-rose-600 text-sm bg-rose-50 border border-rose-200 rounded-xl p-2.5">
          加载失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {creating && (
        <CreateForm
          onCreated={(r) => { setMsg(`已创建调度 ${r.name}（${r.schedule_id}）`); setCreating(false); refresh() }}
        />
      )}

      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-2">
        {(schedules ?? []).length === 0 && !error ? (
          <div className="text-sm text-slate-400 text-center py-10">
            暂无调度任务——点击「新建调度」创建第一个 cron 定时派发
          </div>
        ) : (
          <div className="divide-y divide-slate-50">
            {(schedules ?? []).map((s) => (
              <div key={s.schedule_id} className="px-3 py-3 flex flex-col md:flex-row md:items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                      s.enabled ? 'bg-mint-100 text-mint-700' : 'bg-slate-100 text-slate-500'
                    }`}>{s.enabled ? '启用' : '暂停'}</span>
                    <span className="font-semibold text-slate-800 text-sm">{s.name}</span>
                    <span className="font-mono text-xs bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">{s.cron_expr}</span>
                    <span className="text-xs text-slate-400">{s.role}{s.model ? ` · ${s.model}` : ''}</span>
                  </div>
                  <p className="text-xs text-slate-500 mt-1 truncate" title={s.prompt}>{s.prompt}</p>
                </div>
                <div className="flex items-center gap-3 text-xs text-slate-400 shrink-0">
                  <span>上次: {s.last_run ? formatTime(Number(s.last_run)) : '—'}</span>
                  <span>下次: {s.next_run ? formatTime(Number(s.next_run)) : '—'}</span>
                  <div className="flex gap-1.5">
                    <button disabled={busyId === s.schedule_id}
                      onClick={() => act(s.schedule_id, () => api.toggleSchedule(s.schedule_id), s.enabled ? '暂停' : '启用')}
                      className="px-2.5 py-1 rounded-lg border border-mint-200 text-mint-700 hover:bg-mint-50 disabled:opacity-40">
                      {s.enabled ? '暂停' : '启用'}
                    </button>
                    <button disabled={busyId === s.schedule_id}
                      onClick={() => act(s.schedule_id, () => api.triggerSchedule(s.schedule_id), '手动触发')}
                      className="px-2.5 py-1 rounded-lg bg-sky-100 text-sky-700 hover:bg-sky-200 disabled:opacity-40">触发</button>
                    <button disabled={busyId === s.schedule_id}
                      onClick={() => {
                        if (!window.confirm(`删除调度 [${s.name}] 吗？`)) return
                        act(s.schedule_id, () => api.deleteSchedule(s.schedule_id), '删除')
                      }}
                      className="px-2.5 py-1 rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-40">删除</button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function CreateForm({ onCreated }: { onCreated: (r: ScheduleRecord) => void }) {
  const [name, setName] = useState('')
  const [cron, setCron] = useState('0 9 * * 1-5')
  const [role, setRole] = useState('executor')
  const [prompt, setPrompt] = useState('')
  const [ctx, setCtx] = useState('')
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const valid = useMemo(() => isValidCron(cron), [cron])
  const preview = useMemo(() => (valid ? nextRuns(cron, 3) : []), [cron, valid])

  const submit = async () => {
    if (!name.trim() || !prompt.trim() || !valid) return
    setBusy(true)
    setErr(null)
    try {
      const r = await api.createSchedule({
        name: name.trim(), cron_expr: cron.trim(), role,
        prompt: prompt.trim(), context_memory: ctx || null, model: model || null,
      })
      onCreated(r)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-mint-200 shadow-mint-card p-5 space-y-3">
      <div className="text-xs font-bold text-slate-400 uppercase tracking-wider">新建调度</div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称（如 日报生成）"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <input value={role} onChange={(e) => setRole(e.target.value)} placeholder="角色"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="模型（可选，如 haiku）"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
      </div>
      <div>
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <input value={cron} onChange={(e) => setCron(e.target.value)}
            className={`w-44 px-2.5 py-1.5 text-sm border rounded-lg font-mono focus:outline-none focus:ring-2 ${
              valid ? 'border-mint-200 focus:ring-mint-400' : 'border-rose-300 focus:ring-rose-300'
            }`} />
          {CRON_TEMPLATES.map((t) => (
            <button key={t.expr} onClick={() => setCron(t.expr)}
              className="px-2 py-0.5 text-xs rounded-full bg-mint-50 text-slate-600 hover:bg-mint-100 border border-mint-100">
              {t.label}
            </button>
          ))}
        </div>
        <div className="text-xs">
          {valid && preview.length > 0 ? (
            <span className="text-mint-700">
              未来 3 次运行：{preview.map((ts) => formatTime(ts)).join(' · ')}
              <span className="text-slate-400 ml-2">（{timeAgo(preview[0])}，预览仅供参考，以后端为准）</span>
            </span>
          ) : (
            <span className="text-rose-600">cron 表达式无效（示例：0 9 * * 1-5）</span>
          )}
        </div>
      </div>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3}
        placeholder="定时派发的任务 prompt"
        className="w-full px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
      <textarea value={ctx} onChange={(e) => setCtx(e.target.value)} rows={2}
        placeholder="上下文记忆（可选）"
        className="w-full px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-mint-400" />
      {err && <div className="text-xs text-rose-600">{err}</div>}
      <div className="flex justify-end gap-2">
        <button disabled={busy || !name.trim() || !prompt.trim() || !valid} onClick={submit}
          className="px-5 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">
          创建调度
        </button>
      </div>
    </div>
  )
}
