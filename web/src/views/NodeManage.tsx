import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import TaskList from '../components/TaskList'
import type { WorkerInfo } from '../types'

/** 节点 Agent 面板地址：{host}:{port}/。host 为 0.0.0.0 / ::（监听全网卡）时
 * 回退 127.0.0.1——浏览器访问 0.0.0.0/:: 打不开，本机访问该节点面板必然落在环回。 */
export function panelUrl(w: WorkerInfo): string {
  const host = w.host === '0.0.0.0' || w.host === '::' ? '127.0.0.1' : w.host
  return `http://${host}:${w.port}/`
}

export default function NodeManage() {
  const navigate = useNavigate()
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
  // 内嵌查看：抽屉 iframe 嵌入选中节点的 Agent 面板（面板自向请求，无需 worker 额外 CORS）
  const [embedded, setEmbedded] = useState<WorkerInfo | null>(null)

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
            <div
              key={w.worker_id}
              onClick={() => window.open(panelUrl(w), '_blank', 'noopener')}
              className="bg-white rounded-lg shadow p-3 flex items-center justify-between cursor-pointer hover:bg-slate-50"
              title={`点击打开该节点 Agent 面板（${panelUrl(w)}）`}
            >
              <div>
                <span className="font-medium">{w.worker_id}</span>
                <span className="text-xs text-slate-400 ml-2">
                  {w.role} ·{' '}
                  <a
                    href={panelUrl(w)}
                    target="_blank"
                    rel="noopener"
                    onClick={(e) => e.stopPropagation()}
                    className="text-sky-600 hover:underline"
                    title="新标签页打开该节点 Agent 面板"
                  >
                    {w.host}:{w.port}
                  </a>{' '}
                  · load {w.load}
                </span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={(e) => { e.stopPropagation(); setEmbedded(w) }}
                  className="px-2 py-1 text-xs rounded bg-violet-600 text-white hover:bg-violet-500"
                  title={`抽屉内嵌该节点 Agent 面板（${panelUrl(w)}）`}
                >
                  内嵌查看
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); navigate('/command', { state: { workerId: w.worker_id } }) }}
                  className="px-2 py-1 text-xs rounded bg-mint-600 text-white hover:bg-mint-500"
                  title={`定点派发到此节点（role=${w.role}）`}
                >
                  派发到此节点
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); ping(w.worker_id) }}
                  className="px-2 py-1 text-xs rounded bg-slate-100 hover:bg-slate-200"
                >
                  刷新心跳
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); unregister(w.worker_id) }}
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

      {/* 内嵌查看：抽屉 iframe 嵌入节点 Agent 面板（面板页在节点源内自向请求，
          无 X-Frame-Options 限制即可嵌入，无需 worker 额外 CORS） */}
      {embedded && (
        <div className="fixed inset-0 bg-black/40 z-50 flex justify-end" role="dialog" aria-label={`内嵌节点面板 ${embedded.worker_id}`}>
          <div className="bg-white w-[min(92vw,900px)] h-full flex flex-col shadow-2xl">
            <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-200 shrink-0">
              <span className="font-mono text-sm truncate">{embedded.worker_id}</span>
              <span className="text-xs text-slate-400">{embedded.role} · {embedded.host}:{embedded.port}</span>
              <a
                href={panelUrl(embedded)}
                target="_blank"
                rel="noopener"
                className="ml-auto text-xs text-sky-600 hover:underline"
              >
                新标签页打开 ↗
              </a>
              <button
                onClick={() => setEmbedded(null)}
                className="px-2 py-1 text-xs rounded bg-slate-100 hover:bg-slate-200"
              >
                ✕ 关闭
              </button>
            </div>
            <iframe
              src={panelUrl(embedded)}
              title={`节点 Agent 面板 ${embedded.worker_id}`}
              className="flex-1 w-full border-0 bg-white"
            />
          </div>
        </div>
      )}
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
