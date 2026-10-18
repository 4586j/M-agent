import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { getActiveConnection } from '../api/connections'
import { sessionSelectionKey } from '../api/brainSessions'
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
  // 内嵌查看：抽屉 iframe 嵌入选中节点的 Agent 面板
  const [embedded, setEmbedded] = useState<WorkerInfo | null>(null)
  // 节点详情：展开某行的技能/MCP/工作区状态
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
      {/* 注册新 worker */}
      <section className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]">
        <div className="flex items-center gap-2 mb-3">
          <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
          <h2 className="text-sm font-semibold text-zinc-100 tracking-tight">注册新 worker</h2>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <Input label="worker_id" value={form.worker_id} onChange={(v) => setForm({ ...form, worker_id: v })} />
          <Input label="role" value={form.role} onChange={(v) => setForm({ ...form, role: v })} />
          <Input label="host" value={form.host} onChange={(v) => setForm({ ...form, host: v })} />
          <Input label="port" value={form.port} onChange={(v) => setForm({ ...form, port: v })} />
          <Input label="capabilities(JSON)" value={form.capabilities} onChange={(v) => setForm({ ...form, capabilities: v })} />
        </div>
        <div className="mt-4 flex items-center">
          <button
            type="button"
            onClick={submit}
            className="btn-press px-4 py-1.5 text-xs font-semibold rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 shadow-[0_0_15px_rgba(16,185,129,0.3)] transition duration-150"
          >
            注册
          </button>
          {msg && <span className="ml-3 text-xs text-emerald-400 font-medium font-mono">{msg}</span>}
        </div>
      </section>

      {/* 节点列表与操作 */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-zinc-100 tracking-tight">节点操作</h2>
          <span className="text-xs text-zinc-500 font-mono tabular-nums">
            {workers.data ? `${workers.data.length} 个节点在线` : '加载中…'}
          </span>
        </div>

        <div className="space-y-2.5">
          {(workers.data || []).map((w, idx) => {
            const st = statuses[w.worker_id]
            const err = statusErrors[w.worker_id]
            const isOpen = expanded === w.worker_id
            return (
              <div
                key={w.worker_id}
                style={{ '--stagger': idx } as React.CSSProperties}
                className="spotlight-card stagger-item bg-zinc-900/40 backdrop-blur-xl rounded-xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] overflow-hidden hover:border-emerald-500/30 transition duration-150"
              >
                <div
                  onClick={() => window.open(panelUrl(w), '_blank', 'noopener')}
                  className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-zinc-800/40 transition"
                  title={`点击打开该节点 Agent 面板（${panelUrl(w)}）`}
                >
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
                    <span className="font-semibold text-zinc-100 font-mono text-sm">{w.worker_id}</span>
                    <span className="text-xs text-zinc-400">
                      <span className="px-1.5 py-0.5 rounded bg-zinc-800/80 text-zinc-300 font-mono text-[11px] border border-white/[0.06] mr-1.5">
                        {w.role}
                      </span>
                      <a
                        href={panelUrl(w)}
                        target="_blank"
                        rel="noopener"
                        onClick={(e) => e.stopPropagation()}
                        className="text-emerald-400 font-mono hover:underline mr-1.5"
                        title="新标签页打开该节点 Agent 面板"
                      >
                        {w.host}:{w.port}
                      </a>
                      · <span className="tabular-nums font-mono text-zinc-400">load {w.load}</span>
                      {st && <span className="ml-2 text-zinc-500">· 工作区 {st.workspace.name}</span>}
                    </span>
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); toggleDetail(w.worker_id) }}
                      className={`btn-press px-2.5 py-1 text-xs rounded-lg border transition font-medium ${
                        isOpen
                          ? 'bg-emerald-500/15 border-emerald-500/30 text-emerald-300'
                          : 'bg-zinc-950/70 border-white/[0.08] text-zinc-300 hover:bg-zinc-800'
                      }`}
                      title="展开节点详情：技能 / MCP / 工作区 / run 状态"
                    >
                      {isOpen ? '收起详情' : '详情'}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); setEmbedded(w) }}
                      className="btn-press px-2.5 py-1 text-xs rounded-lg bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 border border-indigo-500/30 font-medium transition"
                      title={`抽屉内嵌该节点 Agent 面板（${panelUrl(w)}）`}
                    >
                      内嵌查看
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); navigate('/command', { state: { workerId: w.worker_id, brainKey: sessionSelectionKey(getActiveConnection()) } }) }}
                      className="btn-press px-2.5 py-1 text-xs rounded-lg bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/30 font-medium transition"
                      title={`定点派发到此节点（role=${w.role}）`}
                    >
                      派发到此节点
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); ping(w.worker_id) }}
                      className="btn-press px-2.5 py-1 text-xs rounded-lg bg-zinc-950/70 border border-white/[0.08] text-zinc-300 hover:bg-zinc-800 transition"
                    >
                      刷新心跳
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); unregister(w.worker_id) }}
                      className="btn-press px-2.5 py-1 text-xs rounded-lg bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 border border-rose-500/20 transition"
                    >
                      注销
                    </button>
                  </div>
                </div>

                {isOpen && (
                  <div className="border-t border-white/[0.06] bg-zinc-950/50 p-4 text-xs">
                    {err && !st ? (
                      <div className="flex flex-wrap items-center gap-2 text-rose-400">
                        <span>状态拉取失败：{err}</span>
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); void fetchStatus(w.worker_id) }}
                          className="px-2 py-0.5 text-xs rounded border border-rose-500/30 hover:bg-rose-500/20"
                        >
                          重试
                        </button>
                      </div>
                    ) : !st ? (
                      <div className="text-zinc-500">加载中…</div>
                    ) : (
                      <div className="grid gap-4 md:grid-cols-2">
                        {/* 工作区规定 */}
                        <div className="space-y-2">
                          <div className="text-xs text-zinc-400 font-medium">
                            对话工作区 {st.workspace.exists ? '' : '（目录缺失）'}
                          </div>
                          <div className="flex gap-2">
                            <input
                              value={wsInputs[w.worker_id] ?? st.workspace.path}
                              onChange={(e) => {
                                e.stopPropagation()
                                setWsInputs((m) => ({ ...m, [w.worker_id]: e.target.value }))
                              }}
                              spellCheck={false}
                              className="flex-1 min-w-0 px-2.5 py-1.5 font-mono text-xs border border-white/[0.08] bg-zinc-950 text-zinc-100 rounded-lg focus:outline-none focus:border-emerald-500/40"
                              placeholder="绝对路径（目录须存在）"
                              title="输入已存在的目录绝对路径后点「切换」"
                            />
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); void switchWorkspace(w.worker_id) }}
                              className="px-3 py-1.5 text-xs rounded-lg bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold whitespace-nowrap transition"
                            >
                              切换
                            </button>
                          </div>
                          <div className="text-[11px] text-zinc-500 font-mono">
                            面板 run：{st.runs.running} 运行中 / {st.runs.total} 总数
                          </div>
                        </div>

                        {/* 技能 / MCP */}
                        <div className="space-y-3">
                          <div>
                            <div className="text-xs text-zinc-400 font-medium mb-1.5">技能（{st.skills.length}）</div>
                            {st.skills.length === 0 ? (
                              <div className="text-zinc-600">无</div>
                            ) : (
                              <div className="flex flex-wrap gap-1.5">
                                {st.skills.map((s) => (
                                  <span
                                    key={s.name}
                                    className="px-2 py-0.5 text-xs rounded-md bg-indigo-500/10 text-indigo-300 border border-indigo-500/20 font-mono"
                                    title={`${s.description}${s.triggers.length ? `\n触发词: ${s.triggers.join('、')}` : ''}`}
                                  >
                                    {s.name}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>

                          <div>
                            <div className="text-xs text-zinc-400 font-medium mb-1.5">MCP 服务（{st.mcp_servers.length}）</div>
                            {st.mcp_servers.length === 0 ? (
                              <div className="text-zinc-600">无</div>
                            ) : (
                              <ul className="space-y-1">
                                {st.mcp_servers.map((m) => (
                                  <li
                                    key={m.name}
                                    className="font-mono text-xs text-zinc-300 truncate"
                                    title={m.command != null ? String(m.command) : m.url != null ? String(m.url) : ''}
                                  >
                                    <span className="text-emerald-400 font-semibold">{m.name}</span>
                                    <span className="text-zinc-500"> — {m.command != null ? String(m.command) : m.url != null ? String(m.url) : '（远程）'}</span>
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
            <div className="text-zinc-500 text-sm py-10 text-center">暂无节点</div>
          )}
        </div>
      </section>

      {/* 全部任务 */}
      <section>
        <h2 className="text-sm font-semibold text-zinc-100 tracking-tight mb-3">全部任务（可取消 / 重派）</h2>
        {tasks.data && tasks.data.length > 0 ? (
          <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-3">
            <TaskList tasks={tasks.data} onChange={tasks.refresh} />
          </div>
        ) : (
          <div className="text-zinc-500 text-sm py-8 text-center">暂无任务</div>
        )}
      </section>

      {/* 内嵌查看抽屉 */}
      {embedded && (
        <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-label={`内嵌节点面板 ${embedded.worker_id}`}>
          <div
            className="fixed inset-0 bg-black/60 backdrop-blur-md transition-opacity"
            onClick={() => setEmbedded(null)}
            aria-hidden="true"
          />
          <div className="relative z-10 bg-zinc-950/95 backdrop-blur-2xl w-[min(92vw,920px)] h-full flex flex-col shadow-2xl border-l border-white/[0.08] ease-spring">
            <div className="flex items-center gap-2.5 px-4 py-3 border-b border-white/[0.07] bg-zinc-900/60 shrink-0">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
              <span className="font-mono text-sm font-semibold text-zinc-100">{embedded.worker_id}</span>
              <span className="text-xs text-zinc-400 font-mono">{embedded.role} · {embedded.host}:{embedded.port}</span>
              <a
                href={panelUrl(embedded)}
                target="_blank"
                rel="noopener"
                className="ml-auto text-xs text-emerald-400 hover:underline font-mono"
              >
                新标签页打开 ↗
              </a>
              <button
                type="button"
                onClick={() => setEmbedded(null)}
                className="btn-press px-2.5 py-1 text-xs rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition"
              >
                ✕ 关闭
              </button>
            </div>
            <iframe
              src={panelUrl(embedded)}
              title={`节点 Agent 面板 ${embedded.worker_id}`}
              className="flex-1 w-full border-0 bg-zinc-950"
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
    <label className="text-[11px] text-zinc-400 font-medium">
      {label}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full px-2.5 py-1.5 text-xs font-mono border border-white/[0.08] rounded-lg bg-zinc-950 text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 transition"
      />
    </label>
  )
}
