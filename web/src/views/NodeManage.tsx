import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'
import TaskList from '../components/TaskList'
import type { NodeStatus, WorkerInfo } from '../types'

/** 节点 Agent 面板地址：{host}:{port}/。host 为 0.0.0.0 / ::（监听全网卡）时
 * 回退 127.0.0.1——浏览器访问 0.0.0.0/:: 打不开，本机访问该节点面板必然落在环回。 */
export function panelUrl(w: WorkerInfo): string {
  const host = w.host === '0.0.0.0' || w.host === '::' ? '127.0.0.1' : w.host
  return `http://${host}:${w.port}/`
}

export default function NodeManage() {
  const navigate = useNavigate()
  const workers = usePolling(() => api.workers(), POLL.workers)
  const tasks = usePolling(() => api.tasks(), POLL.tasks)
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
  // 节点详情：展开某行的技能/MCP/工作区状态（按需经 brain 代理拉取，非心跳轮询）
  const [expanded, setExpanded] = useState<string | null>(null)
  const [statuses, setStatuses] = useState<Record<string, NodeStatus>>({})
  const [statusErrors, setStatusErrors] = useState<Record<string, string>>({})
  const [wsInputs, setWsInputs] = useState<Record<string, string>>({})

  const fetchStatus = async (id: string) => {
    try {
      const s = await api.nodeStatus(id)
      setStatuses((m) => ({ ...m, [id]: s }))
      setWsInputs((m) => ({ ...m, [id]: s.workspace.path }))
      setStatusErrors((m) => ({ ...m, [id]: '' }))
    } catch (e) {
      setStatusErrors((m) => ({ ...m, [id]: e instanceof Error ? e.message : String(e) }))
    }
  }
  const toggleDetail = (id: string) => {
    if (expanded === id) {
      setExpanded(null)
      return
    }
    setExpanded(id)
    void fetchStatus(id)
  }
  const switchWorkspace = async (id: string) => {
    const path = (wsInputs[id] ?? '').trim()
    if (!path) {
      setStatusErrors((m) => ({ ...m, [id]: '请输入工作区路径' }))
      return
    }
    try {
      const res = await api.setNodeWorkspace(id, path)
      setMsg(`节点 ${id} 工作区 → ${res.path}`)
      await fetchStatus(id)
    } catch (e) {
      setStatusErrors((m) => ({ ...m, [id]: e instanceof Error ? e.message : String(e) }))
    }
  }

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
          {(workers.data || []).map((w) => {
            const st = statuses[w.worker_id]
            const err = statusErrors[w.worker_id]
            const isOpen = expanded === w.worker_id
            return (
              <div key={w.worker_id} className="bg-white rounded-lg shadow overflow-hidden">
                <div
                  onClick={() => window.open(panelUrl(w), '_blank', 'noopener')}
                  className="p-3 flex items-center justify-between cursor-pointer hover:bg-slate-50"
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
                      {st && <span className="ml-2 text-slate-300">· 工作区 {st.workspace.name}</span>}
                    </span>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); toggleDetail(w.worker_id) }}
                      className={`px-2 py-1 text-xs rounded border ${isOpen ? 'bg-emerald-50 border-emerald-300 text-emerald-700' : 'bg-slate-50 border-slate-300 hover:bg-slate-100'}`}
                      title="展开节点详情：技能 / MCP / 工作区 / run 状态"
                    >
                      {isOpen ? '收起详情' : '详情'}
                    </button>
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
                {isOpen && (
                  <div className="border-t border-slate-100 p-3 text-sm">
                    {err && !st ? (
                      <div className="flex flex-wrap items-center gap-2 text-rose-600">
                        <span className="text-rose-700">状态拉取失败：{err}</span>
                        <button
                          onClick={(e) => { e.stopPropagation(); void fetchStatus(w.worker_id) }}
                          className="px-2 py-0.5 text-xs rounded border border-rose-300 hover:bg-rose-50"
                        >
                          重试
                        </button>
                      </div>
                    ) : !st ? (
                      <div className="text-slate-400">加载中…</div>
                    ) : (
                      <div className="grid gap-3 md:grid-cols-2">
                        {/* 工作区规定 */}
                        <div>
                          <div className="text-xs text-slate-400 mb-1">对话工作区 {st.workspace.exists ? '' : '（目录缺失）'}</div>
                          <div className="flex gap-2">
                            <input
                              value={wsInputs[w.worker_id] ?? st.workspace.path}
                              onChange={(e) => {
                                e.stopPropagation()
                                setWsInputs((m) => ({ ...m, [w.worker_id]: e.target.value }))
                              }}
                              spellCheck={false}
                              className="flex-1 min-w-0 px-2 py-1 font-mono text-xs border border-slate-300 rounded"
                              placeholder="绝对路径（目录须存在）"
                              title="输入已存在的目录绝对路径后点「切换」"
                            />
                            <button
                              onClick={(e) => { e.stopPropagation(); void switchWorkspace(w.worker_id) }}
                              className="px-2 py-1 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-500 whitespace-nowrap"
                            >
                              切换
                            </button>
                          </div>
                          <div className="text-xs text-slate-400 mt-2">面板 run：{st.runs.running} 运行中 / {st.runs.total} 总数</div>
                        </div>
                        {/* 技能 / MCP */}
                        <div className="grid gap-3">
                          <div>
                            <div className="text-xs text-slate-400 mb-1">技能（{st.skills.length}）</div>
                            {st.skills.length === 0 ? (
                              <div className="text-slate-300">无</div>
                            ) : (
                              <div className="flex flex-wrap gap-1.5">
                                {st.skills.map((s) => (
                                  <span key={s.name} className="px-2 py-0.5 text-xs rounded-full bg-sky-50 text-sky-700 border border-sky-200" title={`${s.description}${s.triggers.length ? `\n触发词: ${s.triggers.join('、')}` : ''}`}>
                                    {s.name}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                          <div>
                            <div className="text-xs text-slate-400 mb-1">MCP 服务（{st.mcp_servers.length}）</div>
                            {st.mcp_servers.length === 0 ? (
                              <div className="text-slate-300">无</div>
                            ) : (
                              <ul className="space-y-0.5">
                                {st.mcp_servers.map((m) => (
                                  <li key={m.name} className="font-mono text-xs text-slate-600 truncate"
                                      title={m.command != null ? String(m.command) : m.url != null ? String(m.url) : ''}>
                                    {m.name}
                                    <span className="text-slate-400"> — {m.command != null ? String(m.command) : m.url != null ? String(m.url) : '（远程）'}</span>
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
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
