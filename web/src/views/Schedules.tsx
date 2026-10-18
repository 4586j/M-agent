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
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-xs text-zinc-400">
          定时任务按 cron 表达式自动派发（经安全关卡①检验，与其他派发同链路）
        </p>
        <button
          type="button"
          onClick={() => setCreating(!creating)}
          className="btn-press px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
        >
          {creating ? '收起表单' : '+ 新建调度'}
        </button>
      </div>

      {msg && (
        <div className="text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-2 font-mono">
          {msg}
        </div>
      )}
      {error && (
        <div className="text-rose-400 text-xs bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">
          加载失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {creating && (
        <CreateForm
          onCreated={(r) => { setMsg(`已创建调度 ${r.name}（${r.schedule_id}）`); setCreating(false); refresh() }}
        />
      )}

      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-2">
        {(schedules ?? []).length === 0 && !error ? (
          <div className="text-sm text-zinc-500 text-center py-12">
            暂无调度任务——点击「新建调度」创建第一个 cron 定时派发
          </div>
        ) : (
          <div className="divide-y divide-white/[0.04]">
            {(schedules ?? []).map((s) => (
              <div
                key={s.schedule_id}
                className="px-4 py-3.5 flex flex-col md:flex-row md:items-center gap-3 hover:bg-zinc-800/40 rounded-xl transition duration-150"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-bold font-mono ${
                        s.enabled
                          ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                          : 'bg-zinc-800/80 text-zinc-500 border border-white/[0.06]'
                      }`}
                    >
                      {s.enabled ? '启用' : '暂停'}
                    </span>
                    <span className="font-semibold text-zinc-100 text-sm">{s.name}</span>
                    <span className="font-mono text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-2 py-0.5 rounded">
                      {s.cron_expr}
                    </span>
                    <span className="text-xs text-zinc-400 font-mono">
                      {s.role}{s.model ? ` · ${s.model}` : ''}
                    </span>
                  </div>
                  <p className="text-xs text-zinc-400 mt-1.5 truncate font-mono" title={s.prompt}>{s.prompt}</p>
                </div>

                <div className="flex items-center gap-3 text-xs text-zinc-400 shrink-0 font-mono flex-wrap">
                  <span className="tabular-nums">上次: {s.last_run ? formatTime(Number(s.last_run)) : '—'}</span>
                  <span className="tabular-nums">下次: {s.next_run ? formatTime(Number(s.next_run)) : '—'}</span>
                  <div className="flex gap-1.5">
                    <button
                      type="button"
                      disabled={busyId === s.schedule_id}
                      onClick={() => act(s.schedule_id, () => api.toggleSchedule(s.schedule_id), s.enabled ? '暂停' : '启用')}
                      className="btn-press px-3 py-1 rounded-lg border border-white/[0.08] bg-zinc-950/70 text-zinc-300 hover:bg-zinc-800 hover:text-emerald-400 text-xs font-medium disabled:opacity-40 transition"
                    >
                      {s.enabled ? '暂停' : '启用'}
                    </button>
                    <button
                      type="button"
                      disabled={busyId === s.schedule_id}
                      onClick={() => act(s.schedule_id, () => api.triggerSchedule(s.schedule_id), '手动触发')}
                      className="btn-press px-3 py-1 rounded-lg bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 border border-indigo-500/30 text-xs font-medium disabled:opacity-40 transition"
                    >
                      触发
                    </button>
                    <button
                      type="button"
                      disabled={busyId === s.schedule_id}
                      onClick={() => {
                        if (!window.confirm(`删除调度 [${s.name}] 吗？`)) return
                        act(s.schedule_id, () => api.deleteSchedule(s.schedule_id), '删除')
                      }}
                      className="btn-press px-3 py-1 rounded-lg border border-rose-500/20 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 text-xs font-medium disabled:opacity-40 transition"
                    >
                      删除
                    </button>
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
    <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.08] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5 space-y-3.5">
      <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
        新建调度
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="名称（如 日报生成）"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
        <input
          value={role}
          onChange={(e) => setRole(e.target.value)}
          placeholder="角色"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="模型（可选，如 haiku）"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
      </div>

      <div>
        <div className="flex items-center gap-2 flex-wrap mb-1.5">
          <input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            className={`w-44 px-3 py-1.5 text-xs border rounded-xl font-mono bg-zinc-950 text-zinc-100 focus:outline-none ${
              valid
                ? 'border-white/[0.08] focus:border-emerald-500/40 focus:ring-1 focus:ring-emerald-500/20'
                : 'border-rose-500/50 text-rose-300 focus:border-rose-500'
            }`}
          />
          {CRON_TEMPLATES.map((t) => (
            <button
              key={t.expr}
              type="button"
              onClick={() => setCron(t.expr)}
              className="px-2.5 py-1 text-xs rounded-lg bg-zinc-950/70 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 border border-white/[0.06] transition"
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="text-xs font-mono">
          {valid && preview.length > 0 ? (
            <span className="text-emerald-400">
              未来 3 次运行：{preview.map((ts) => formatTime(ts)).join(' · ')}
              <span className="text-zinc-500 ml-2">（{timeAgo(preview[0])}，预览仅供参考，以后端为准）</span>
            </span>
          ) : (
            <span className="text-rose-400">cron 表达式无效（示例：0 9 * * 1-5）</span>
          )}
        </div>
      </div>

      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        rows={3}
        placeholder="定时派发的任务 prompt"
        className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
      />
      <textarea
        value={ctx}
        onChange={(e) => setCtx(e.target.value)}
        rows={2}
        placeholder="上下文记忆（可选）"
        className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
      />
      {err && <div className="text-xs text-rose-400 font-mono">{err}</div>}
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          disabled={busy || !name.trim() || !prompt.trim() || !valid}
          onClick={submit}
          className="px-5 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
        >
          创建调度
        </button>
      </div>
    </div>
  )
}
