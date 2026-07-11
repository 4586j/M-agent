import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import StatBar from '../components/StatBar'
import WorkerCard from '../components/WorkerCard'

export default function Dashboard() {
  const stats = usePolling(() => api.stats(), 5000)
  const workers = usePolling(() => api.workers(), 5000)
  const timeout = stats.data?.heartbeat_timeout ?? 45

  return (
    <div>
      <StatBar stats={stats.data} />
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold">节点列表</h2>
        <span className="text-xs text-slate-400">每 5s 自动刷新 · 点击节点展开任务</span>
      </div>
      {workers.error && (
        <div className="text-rose-600 text-sm mb-3 bg-rose-50 border border-rose-200 rounded p-2">
          大脑连接失败：{workers.error}（请确认 brain_api 已以 <code>ORCH_MODE=remote</code> 启动）
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {(workers.data || []).map((w) => (
          <WorkerCard key={w.worker_id} worker={w} heartbeatTimeout={timeout} />
        ))}
        {workers.data && workers.data.length === 0 && !workers.error && (
          <div className="text-slate-400 text-sm col-span-2">
            暂无活跃节点。请在「节点管理」注册 worker，或确认 worker 已启动并自动心跳。
          </div>
        )}
      </div>
    </div>
  )
}
