import { useMemo, useState } from 'react'
import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import type { ApprovalMeta, TaskRecord } from '../types'
import StatBar from '../components/StatBar'
import CostPanel from '../components/CostPanel'
import WorkerCard from '../components/WorkerCard'
import TaskDetailDrawer from '../components/TaskDetailDrawer'

/** 总览：10 秒内回答"系统健康吗？有什么需要我处理的？"（统计条 → 待办中心 → 分布/成本 → 节点） */
export default function Dashboard() {
  const stats = usePolling(() => api.stats(), 5000)
  const workers = usePolling(() => api.workers(), 5000)
  const tasks = usePolling<TaskRecord[]>(() => api.tasks(), 5000)
  const [selected, setSelected] = useState<TaskRecord | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const timeout = stats.data?.heartbeat_timeout ?? 45

  // 缓存命中率聚合：Σcache_read / Σ(input+cache_read+cache_creation)
  const cacheHit = useMemo(() => {
    let read = 0
    let total = 0
    let has = false
    for (const t of tasks.data || []) {
      const u = t.meta?.usage
      if (!u) continue
      const r = typeof u.cache_read_input_tokens === 'number' ? u.cache_read_input_tokens : 0
      const i = typeof u.input_tokens === 'number' ? u.input_tokens : 0
      const c = typeof u.cache_creation_input_tokens === 'number' ? u.cache_creation_input_tokens : 0
      if (r > 0) has = true
      read += r
      total += r + i + c
    }
    return has && total > 0 ? read / total : null
  }, [tasks.data])

  const approvals = useMemo(
    () => (tasks.data || []).filter((t) => t.status === 'awaiting_approval').slice(0, 5),
    [tasks.data],
  )
  const failures = useMemo(
    () => (tasks.data || []).filter((t) => t.status === 'failed').slice(0, 3),
    [tasks.data],
  )
  const hasAction = approvals.length > 0 || failures.length > 0

  const inline = async (task: TaskRecord, kind: 'approve' | 'reject' | 'redispatch') => {
    setBusyId(task.task_id)
    setMsg(null)
    try {
      if (kind === 'approve') {
        const r = await api.approveTask(task.task_id)
        setMsg(r.approved === true
          ? `已放行 ${task.task_id}${r.dispatched_task_id ? ` → ${r.dispatched_task_id}` : ''}`
          : `放行未成功：${String(r.error ?? r.status)}`)
      } else if (kind === 'reject') {
        if (!window.confirm(`确定拒绝任务 [${task.task_id}] 吗？`)) return
        await api.rejectTask(task.task_id)
        setMsg(`已拒绝 ${task.task_id}`)
      } else {
        const r = await api.redispatchTask(task.task_id)
        setMsg(`已重派 ${task.task_id} → ${r.task_id}`)
      }
      tasks.refresh()
    } catch (e) {
      setMsg(`${kind} 失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-5">
      <StatBar stats={stats.data} cacheHit={cacheHit} />
      {stats.error && (
        <div className="text-rose-600 text-sm bg-rose-50 border border-rose-200 rounded-xl p-2.5">
          大脑连接失败：{stats.error}（请确认 brain_api 已启动，带鉴权部署点右上角 🔑 配置 Key）
        </div>
      )}

      {/* 待办中心：审批 + 重试耗尽失败，10 秒内决策 */}
      {hasAction && (
        <div className="bg-gradient-to-r from-amber-50/70 via-white to-white border border-amber-200/80 rounded-2xl p-5 shadow-mint-sm">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-amber-500" />
              <h3 className="text-sm font-bold text-slate-800 tracking-tight">待办中心</h3>
              <span className="text-xs bg-amber-100 text-amber-800 px-2 py-0.5 rounded-full font-bold">
                {approvals.length + failures.length} 项待处置
              </span>
            </div>
            {msg && <span className="text-xs text-mint-700 font-medium">{msg}</span>}
          </div>
          <div className="space-y-2.5">
            {approvals.map((t) => (
              <div key={t.task_id} className="bg-white border border-amber-200/60 rounded-xl p-3.5 flex flex-col md:flex-row md:items-center justify-between gap-3 shadow-sm hover:border-amber-300 transition">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-slate-800 text-sm font-mono">{t.task_id}</span>
                    <span className="text-xs bg-amber-50 text-amber-700 border border-amber-200 px-2 py-0.5 rounded font-medium">待人工审批</span>
                    <span className="text-xs text-slate-400">{t.role}</span>
                  </div>
                  <p className="text-xs text-slate-500 mt-1 truncate" title={t.prompt}>
                    {(t.meta as (Record<string, unknown> & { approval?: ApprovalMeta }) | undefined)?.approval?.verdict?.reason
                      || t.verdict?.reason || t.prompt}
                  </p>
                </div>
                <div className="flex items-center gap-2 self-end md:self-auto shrink-0">
                  <button onClick={() => setSelected(t)} className="px-3 py-1.5 rounded-lg border border-slate-200 hover:border-mint-400 hover:text-mint-700 text-xs font-medium text-slate-600 transition bg-white">
                    查看 / 修订
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'approve')}
                    className="px-3.5 py-1.5 rounded-lg bg-mint-600 hover:bg-mint-500 text-white text-xs font-semibold shadow-sm transition disabled:opacity-40"
                  >
                    放行
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'reject')}
                    className="px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-rose-50 hover:text-rose-600 text-xs font-medium text-slate-600 transition disabled:opacity-40"
                  >
                    拒绝
                  </button>
                </div>
              </div>
            ))}
            {failures.map((t) => (
              <div key={t.task_id} className="bg-white border border-rose-100 rounded-xl p-3.5 flex flex-col md:flex-row md:items-center justify-between gap-3 shadow-sm transition">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-slate-800 text-sm font-mono">{t.task_id}</span>
                    <span className="text-xs bg-rose-50 text-rose-700 border border-rose-200 px-2 py-0.5 rounded font-medium">失败</span>
                    <span className="text-xs text-slate-400">{t.role}</span>
                  </div>
                  <p className="text-xs text-slate-500 mt-1 truncate" title={String(t.error ?? '')}>
                    {String(t.error ?? t.prompt)}
                  </p>
                </div>
                <div className="flex items-center gap-2 self-end md:self-auto shrink-0">
                  <button onClick={() => setSelected(t)} className="px-3 py-1.5 rounded-lg border border-slate-200 hover:border-mint-400 hover:text-mint-700 text-xs font-medium text-slate-600 transition bg-white">
                    查看错误
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'redispatch')}
                    className="px-3.5 py-1.5 rounded-lg bg-mint-600 hover:bg-mint-500 text-white text-xs font-semibold shadow-sm transition disabled:opacity-40"
                  >
                    重派
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 状态分布 + 成本 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div className="bg-white rounded-2xl p-5 border border-mint-100 shadow-mint-card">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-bold text-slate-800">状态分布</h3>
            <span className="text-xs text-slate-400">总计 {stats.data?.task_total ?? 0} 条</span>
          </div>
          <DistributionBars stats={stats.data?.task_status} />
        </div>
        <div className="lg:col-span-2">
          <CostPanel cost={stats.data?.cost} />
        </div>
      </div>

      {/* 节点列表 */}
      <div className="flex items-center justify-between mb-1">
        <h2 className="text-base font-semibold">节点列表</h2>
        <span className="text-xs text-slate-400">每 5s 自动刷新 · 点击节点展开任务</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {(workers.data || []).map((w) => (
          <WorkerCard key={w.worker_id} worker={w} heartbeatTimeout={timeout} />
        ))}
        {workers.data && workers.data.length === 0 && !workers.error && (
          <div className="text-slate-400 text-sm col-span-2">
            暂无活跃节点。单机形态可直接用命令台派发；跨主机请在「节点管理」注册 worker。
          </div>
        )}
      </div>

      <TaskDetailDrawer
        task={selected}
        open={selected != null}
        onClose={() => setSelected(null)}
        onChange={() => { tasks.refresh(); stats.refresh() }}
      />
    </div>
  )
}

function DistributionBars({ stats }: { stats?: Record<string, number> }) {
  const total = Object.values(stats || {}).reduce((s, v) => s + v, 0)
  const order: [string, string][] = [
    ['done', 'bg-mint-500'],
    ['running', 'bg-sky-400'],
    ['pending', 'bg-slate-300'],
    ['awaiting_approval', 'bg-amber-400'],
    ['failed', 'bg-rose-400'],
    ['lost', 'bg-slate-400'],
    ['cancelled', 'bg-zinc-400'],
  ]
  const entries = order.filter(([k]) => (stats?.[k] ?? 0) > 0)
  if (total === 0 || entries.length === 0) {
    return <div className="text-xs text-slate-400">暂无任务数据</div>
  }
  const label: Record<string, string> = {
    done: '成功', running: '进行中', pending: '排队', awaiting_approval: '待审批',
    failed: '失败', lost: '失联', cancelled: '已取消',
  }
  return (
    <div className="space-y-3">
      {entries.map(([k, color]) => {
        const v = stats?.[k] ?? 0
        const pct = Math.round((v / total) * 1000) / 10
        return (
          <div key={k}>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-slate-600 font-medium">{label[k] || k} ({v})</span>
              <span className="text-slate-500 font-semibold">{pct}%</span>
            </div>
            <div className="w-full bg-slate-100 rounded-full h-2 overflow-hidden">
              <div className={`${color} h-2 rounded-full transition-all duration-500`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
