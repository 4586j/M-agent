import { useState } from 'react'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import TaskList from '../components/TaskList'

export default function NodeManage() {
  const workers = usePolling(() => api.workers(), 5000)
  const tasks = usePolling(() => api.tasks(), 5000)
  const [form, setForm] = useState({
    worker_id: '',
    role: 'executor',
    host: '127.0.0.1',
    port: '8001',
    capabilities: '{}',
  })
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async () => {
    setMsg(null)
    if (!form.worker_id.trim()) {
      setMsg('worker_id 不能为空')
      return
    }
    let caps: Record<string, unknown> = {}
    try {
      caps = JSON.parse(form.capabilities || '{}')
    } catch {
      setMsg('capabilities 非合法 JSON')
      return
    }
    try {
      await api.register({
        worker_id: form.worker_id,
        role: form.role,
        host: form.host,
        port: Number(form.port),
        capabilities: caps,
      })
      setMsg(`已注册 ${form.worker_id}`)
      workers.refresh()
    } catch (e) {
      setMsg('注册失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }

  const unregister = async (id: string) => {
    if (!confirm(`确认注销节点 ${id}？`)) return
    try {
      await api.deleteWorker(id)
      workers.refresh()
    } catch (e) {
      alert(String(e))
    }
  }
  const ping = async (id: string) => {
    try {
      await api.heartbeat(id)
      workers.refresh()
    } catch (e) {
      alert(String(e))
    }
  }

  return (
    <div className="space-y-6">
      <section className="bg-white rounded-lg shadow p-4">
        <h2 className="text-base font-semibold mb-3">注册新 worker</h2>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          <Input label="worker_id" value={form.worker_id} onChange={(v) => setForm({ ...form, worker_id: v })} />
          <Input label="role" value={form.role} onChange={(v) => setForm({ ...form, role: v })} />
          <Input label="host" value={form.host} onChange={(v) => setForm({ ...form, host: v })} />
          <Input label="port" value={form.port} onChange={(v) => setForm({ ...form, port: v })} />
          <Input label="capabilities(JSON)" value={form.capabilities} onChange={(v) => setForm({ ...form, capabilities: v })} />
        </div>
        <button
          onClick={submit}
          className="mt-3 px-4 py-1.5 text-sm rounded bg-sky-600 text-white hover:bg-sky-700"
        >
          注册
        </button>
        {msg && <span className="ml-3 text-sm text-slate-500">{msg}</span>}
      </section>

      <section>
        <h2 className="text-base font-semibold mb-3">节点操作</h2>
        <div className="space-y-2">
          {(workers.data || []).map((w) => (
            <div key={w.worker_id} className="bg-white rounded-lg shadow p-3 flex items-center justify-between">
              <div>
                <span className="font-medium">{w.worker_id}</span>
                <span className="text-xs text-slate-400 ml-2">
                  {w.role} · {w.host}:{w.port} · load {w.load}
                </span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => ping(w.worker_id)}
                  className="px-2 py-1 text-xs rounded bg-slate-100 hover:bg-slate-200"
                >
                  刷新心跳
                </button>
                <button
                  onClick={() => unregister(w.worker_id)}
                  className="px-2 py-1 text-xs rounded bg-rose-100 text-rose-700 hover:bg-rose-200"
                >
                  注销
                </button>
              </div>
            </div>
          ))}
          {workers.data && workers.data.length === 0 && (
            <div className="text-slate-400 text-sm">暂无节点</div>
          )}
        </div>
      </section>

      <section>
        <h2 className="text-base font-semibold mb-3">全部任务（可取消 / 重派）</h2>
        {tasks.data && tasks.data.length > 0 ? (
          <div className="bg-white rounded-lg shadow p-3">
            <TaskList tasks={tasks.data} onChange={tasks.refresh} />
          </div>
        ) : (
          <div className="text-slate-400 text-sm">暂无任务</div>
        )}
      </section>
    </div>
  )
}

function Input({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="text-xs text-slate-500">
      {label}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded"
      />
    </label>
  )
}
