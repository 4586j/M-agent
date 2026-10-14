import { useMemo, useState } from 'react'
import { usePolling } from '../hooks/usePolling'
import { useSharedStats } from '../hooks/useSharedStats'
import { POLL } from '../polling'
import { api } from '../api/client'
import type { ApprovalMeta, TaskRecord } from '../types'
import { statusDot } from '../types'
import StatBar from '../components/StatBar'
import CostPanel from '../components/CostPanel'
import WorkerCard from '../components/WorkerCard'
import TaskDetailDrawer from '../components/TaskDetailDrawer'

/** 总览：10 秒内回答"系统健康吗？有什么需要我处理的？"（统计条 → 待办中心 → 节点矩阵 + 状态/成本） */
export default function Dashboard() {
  const stats = useSharedStats()
  const workers = usePolling(() => api.workers(), POLL.workers)
  const tasks = usePolling<TaskRecord[]>(() => api.tasks(), POLL.tasks)
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
    <div className="space-y-6">
      <StatBar stats={stats.data} cacheHit={cacheHit} />

      {stats.error && (
        <div className="text-rose-400 text-xs bg-rose-500/10 border border-rose-500/25 rounded-2xl p-3 font-mono flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-rose-500" />
          <span>大脑连接失败：{stats.error}（请确认 brain_api 已启动，带鉴权部署点右上角 🔑 配置 Key）</span>
        </div>
      )}

      {/* 待办中心：审批 + 失败重派，高光琥珀微光拦截条 */}
      {hasAction && (
        <div className="bg-zinc-900/60 backdrop-blur-xl border border-amber-500/30 rounded-2xl p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05),0_0_20px_rgba(245,158,11,0.06)]">
          <div className="flex items-center justify-between mb-3.5">
            <div className="flex items-center gap-2.5">
              <span className="w-2.5 h-2.5 rounded-full bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.5)]" />
              <h3 className="text-sm font-bold text-zinc-100 tracking-tight">待办中心</h3>
              <span className="text-xs bg-amber-500/15 text-amber-300 border border-amber-500/25 px-2.5 py-0.5 rounded-full font-mono font-semibold">
                {approvals.length + failures.length} 项待处置
              </span>
            </div>
            {msg && <span className="text-xs text-emerald-400 font-mono">{msg}</span>}
          </div>
          <div className="space-y-2.5">
            {approvals.map((t) => (
              <div key={t.task_id} className="bg-zinc-950/80 border border-amber-500/20 rounded-xl p-3.5 flex flex-col md:flex-row md:items-center justify-between gap-3 shadow-sm hover:border-amber-500/40 transition">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-zinc-100 text-sm font-mono">{t.task_id}</span>
                    <span className="text-xs bg-amber-500/15 text-amber-300 border border-amber-500/30 px-2 py-0.5 rounded font-mono font-medium">待人工审批</span>
                    <span className="text-xs text-zinc-500 font-mono">{t.role}</span>
                  </div>
                  <p className="text-xs text-zinc-400 mt-1.5 truncate" title={t.prompt}>
                    {(t.meta as (Record<string, unknown> & { approval?: ApprovalMeta }) | undefined)?.approval?.verdict?.reason
                      || t.verdict?.reason || t.prompt}
                  </p>
                </div>
                <div className="flex items-center gap-2 self-end md:self-auto shrink-0">
                  <button onClick={() => setSelected(t)} className="btn-press px-3 py-1.5 rounded-lg border border-white/10 hover:border-emerald-500/40 hover:text-emerald-400 text-xs font-medium text-zinc-300 transition bg-zinc-900">
                    查看 / 修订
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'approve')}
                    className="btn-press px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-zinc-950 text-xs font-semibold shadow-sm transition disabled:opacity-40 font-mono shadow-[0_0_12px_rgba(16,185,129,0.2)]"
                  >
                    放行
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'reject')}
                    className="btn-press px-3.5 py-1.5 rounded-lg bg-zinc-900 hover:bg-rose-500/10 hover:text-rose-400 text-xs font-medium text-zinc-400 border border-white/10 hover:border-rose-500/30 transition disabled:opacity-40"
                  >
                    拒绝
                  </button>
                </div>
              </div>
            ))}
            {failures.map((t) => (
              <div key={t.task_id} className="bg-zinc-950/80 border border-rose-500/20 rounded-xl p-3.5 flex flex-col md:flex-row md:items-center justify-between gap-3 shadow-sm hover:border-rose-500/40 transition">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-zinc-100 text-sm font-mono">{t.task_id}</span>
                    <span className="text-xs bg-rose-500/15 text-rose-300 border border-rose-500/30 px-2 py-0.5 rounded font-mono font-medium">失败</span>
                    <span className="text-xs text-zinc-500 font-mono">{t.role}</span>
                  </div>
                  <p className="text-xs text-zinc-400 mt-1.5 truncate" title={String(t.error ?? '')}>
                    {String(t.error ?? t.prompt)}
                  </p>
                </div>
                <div className="flex items-center gap-2 self-end md:self-auto shrink-0">
                  <button onClick={() => setSelected(t)} className="btn-press px-3 py-1.5 rounded-lg border border-white/10 hover:border-emerald-500/40 hover:text-emerald-400 text-xs font-medium text-zinc-300 transition bg-zinc-900">
                    查看错误
                  </button>
                  <button
                    disabled={busyId === t.task_id}
                    onClick={() => inline(t, 'redispatch')}
                    className="btn-press px-3.5 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-zinc-950 text-xs font-semibold shadow-sm transition disabled:opacity-40 font-mono shadow-[0_0_12px_rgba(16,185,129,0.2)]"
                  >
                    重派
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 中部左右分栏：左侧 2/3 为 Worker 算力节点矩阵，右侧 1/3 为成本看板与状态分布 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
        {/* 左侧 2/3：算力节点矩阵 */}
        <div className="lg:col-span-2 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-zinc-100 tracking-tight flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
              节点矩阵与算力
            </h2>
            <span className="text-xs text-zinc-500 font-mono">5s 实时心跳轮询</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {(workers.data || []).map((w) => (
              <WorkerCard key={w.worker_id} worker={w} heartbeatTimeout={timeout} />
            ))}
            {workers.data && workers.data.length === 0 && !workers.error && (
              <div className="text-zinc-500 text-xs font-mono col-span-2 bg-zinc-900/40 border border-white/[0.06] rounded-2xl p-8 text-center">
                暂无活跃节点。单机形态可直接在命令台派发；集群模式请在「节点管理」注册 worker。
              </div>
            )}
          </div>
        </div>

        {/* 右侧 1/3：成本看板与状态分布 */}
        <div className="space-y-6">
          <CostPanel cost={stats.data?.cost} />

          <div className="bg-zinc-900/60 backdrop-blur-xl rounded-2xl p-5 border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-bold text-zinc-100 tracking-tight flex items-center gap-2">
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-400" />
                状态分布
              </h3>
              <span className="text-xs text-zinc-500 font-mono">总计 {stats.data?.task_total ?? 0}</span>
            </div>
            <DistributionBars stats={stats.data?.task_status} />
          </div>
        </div>
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
  const order: string[] = [
    'done', 'running', 'pending', 'awaiting_approval', 'failed', 'lost', 'cancelled',
  ]
  const entries = order.filter((k) => (stats?.[k] ?? 0) > 0)
  if (total === 0 || entries.length === 0) {
    return <div className="text-xs text-zinc-500 font-mono py-4 text-center">暂无任务数据</div>
  }
  const label: Record<string, string> = {
    done: '成功', running: '进行中', pending: '排队', awaiting_approval: '待审批',
    failed: '失败', lost: '失联', cancelled: '已取消',
  }
  return (
    <div className="space-y-3">
      {entries.map((k) => {
        const v = stats?.[k] ?? 0
        const pct = Math.round((v / total) * 1000) / 10
        return (
          <div key={k}>
            <div className="flex justify-between text-xs mb-1.5 font-mono">
              <span className="text-zinc-300 font-medium">{label[k] || k} ({v})</span>
              <span className="text-zinc-400 font-semibold">{pct}%</span>
            </div>
            <div className="w-full bg-zinc-950 rounded-full h-2 overflow-hidden border border-white/[0.04]">
              <div className={`${statusDot(k)} h-2 rounded-full transition-all duration-500`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
