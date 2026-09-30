import { useMemo, useState } from 'react'
import type {
  ExperienceRecord, MemoryCoreItem, ProposalRecord, ReflectionReport, TaskRecord,
} from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'
import { timeAgo } from '../utils'
import StatusBadge from '../components/StatusBadge'

type Tab = 'core' | 'context' | 'experience' | 'proposal' | 'knowledge'

const TABS: { key: Tab; label: string }[] = [
  { key: 'core', label: '核心记忆' },
  { key: 'context', label: '任务上下文' },
  { key: 'experience', label: '经验库' },
  { key: 'proposal', label: '演化提案' },
  { key: 'knowledge', label: '知识库' },
]

/** 记忆系统四层资产库 + 知识库（docs/客户端界面设计.md §3.5） */
export default function Memory() {
  const [tab, setTab] = useState<Tab>('core')
  return (
    <div>
      {/* 分段选择器 */}
      <div className="inline-flex p-1 bg-mint-50/70 border border-mint-200/50 rounded-xl mb-5">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-1.5 text-sm rounded-lg transition ${
              tab === t.key
                ? 'bg-white shadow-mint-sm border border-mint-200/60 font-semibold text-mint-700'
                : 'font-medium text-slate-600 hover:text-mint-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'core' && <CoreMemTab />}
      {tab === 'context' && <ContextTab />}
      {tab === 'experience' && <ExperienceTab />}
      {tab === 'proposal' && <ProposalTab />}
      {tab === 'knowledge' && <KnowledgeTab />}
    </div>
  )
}

/* ---------------- Tab 5 知识库 ---------------- */

function KnowledgeTab() {
  const { data, refresh, error } = usePolling(() => api.knowledge(), POLL.services)
  const docs = data?.docs ?? []
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<{ doc_id: string; title: string; snippet: string }[] | null>(null)
  const [searching, setSearching] = useState(false)

  const upload = async () => {
    if (!content.trim()) return
    setBusy(true)
    setMsg(null)
    try {
      await api.addKnowledge({ title: title.trim() || '未命名文档', content })
      setMsg('已上传：派发时会自动检索相关片段注入上下文')
      setTitle('')
      setContent('')
      refresh()
    } catch (e) {
      setMsg(`上传失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string, name: string) => {
    if (!window.confirm(`删除文档「${name}」？`)) return
    try {
      await api.deleteKnowledge(id)
      refresh()
    } catch (e) {
      setMsg(`删除失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const search = async () => {
    if (!query.trim()) return
    setSearching(true)
    try {
      setResults((await api.searchKnowledge(query, 5)).results)
    } catch {
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <section className="bg-white rounded-lg shadow p-4">
        <h3 className="text-base font-semibold mb-1">上传文档</h3>
        <p className="text-xs text-slate-400 mb-3">
          项目规范 / API 文档 / 运维手册等确定性资料。派发时自动检索相关片段注入任务上下文（空库零影响）。
        </p>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="文档标题"
          className="w-full px-2 py-1 text-sm border border-slate-300 rounded mb-2"
        />
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={8}
          placeholder="粘贴文档正文（Markdown/纯文本均可，按段落自动切块）"
          className="w-full px-2 py-1 text-sm border border-slate-300 rounded font-mono"
        />
        <div className="flex items-center gap-2 mt-2">
          <button
            onClick={upload}
            disabled={busy || !content.trim()}
            className="px-4 py-1.5 text-sm rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40"
          >
            {busy ? '上传中…' : '上传文档'}
          </button>
          {msg && <span className="text-xs text-slate-500">{msg}</span>}
        </div>
      </section>

      <section className="bg-white rounded-lg shadow p-4">
        <h3 className="text-base font-semibold mb-3">文档（{docs.length}）与检索预览</h3>
        <div className="flex items-center gap-1.5 mb-3">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入查询，预览将被注入的片段…"
            className="flex-1 px-2 py-1 text-sm border border-slate-300 rounded"
          />
          <button
            onClick={search}
            disabled={searching || !query.trim()}
            className="px-3 py-1 text-xs rounded bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-40"
          >
            {searching ? '检索中…' : '检索'}
          </button>
        </div>
        {results && (
          <div className="space-y-1.5 mb-3 max-h-52 overflow-y-auto">
            {results.length === 0 && (
              <div className="text-xs text-slate-400">没有命中片段</div>
            )}
            {results.map((r, i) => (
              <div key={`${r.doc_id}-${i}`} className="border border-mint-100 rounded-lg px-2 py-1.5 bg-mint-50/40">
                <div className="text-xs font-medium text-mint-700">{r.title}</div>
                <div className="text-xs text-slate-600 line-clamp-3">{r.snippet}</div>
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="text-xs text-rose-600 bg-rose-50 border border-rose-200 rounded p-2 mb-2">
            加载失败：{error}
          </div>
        )}
        {docs.length === 0 && !error ? (
          <div className="text-slate-400 text-sm">知识库为空——上传第一份文档</div>
        ) : (
          <div className="space-y-1.5">
            {docs.map((d) => (
              <div key={d.doc_id} className="flex items-center gap-2 border border-slate-100 rounded p-2">
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-slate-700 truncate">{d.title}</div>
                  <div className="text-xs text-slate-400">
                    {d.chunk_count} 块 · {d.content_chars} 字 · 更新于 {timeAgo(d.updated_at)}
                  </div>
                </div>
                <button
                  onClick={() => { if (window.confirm(`删除文档「${d.title}」？`)) remove(d.doc_id, d.title) }}
                  className="px-2 py-1 text-xs text-slate-400 hover:text-rose-600"
                >
                  删除
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

/* ---------------- Tab 1 核心记忆 ---------------- */

function CoreMemTab() {
  const { data, refresh, error } = usePolling(() => api.memoryCore(), POLL.services)
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const memories: MemoryCoreItem[] = data?.memories ?? []
  const current = memories.find((m) => m.name === selected) ?? memories[0] ?? null

  const save = async (name: string, content: string) => {
    setBusy(true)
    setMsg(null)
    try {
      await api.memoryCorePut(name, content)
      setMsg(`已写入 ${name}（共识门控：核心记忆应由三方共识流程产出）`)
      setEditing(null)
      refresh()
    } catch (e) {
      setMsg(`写入失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    if (!window.confirm(
      `确定删除核心记忆 [${name}] 吗？\n核心记忆是跨任务共识资产——删除也应走三方共识门控，确认继续？`,
    )) return
    setBusy(true)
    setMsg(null)
    try {
      await api.memoryCoreDelete(name)
      setSelected(null)
      setMsg(`已删除 ${name}`)
      refresh()
    } catch (e) {
      setMsg(`删除失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-3">
        <div className="flex items-center justify-between px-1 mb-2">
          <span className="text-xs font-bold text-slate-400 uppercase tracking-wider">记忆列表</span>
          <button onClick={() => { setCreating(true); setEditing(null); setNewName('') }}
            className="text-xs text-mint-700 hover:underline font-medium">+ 新建</button>
        </div>
        {error && <div className="text-xs text-rose-600 px-1">{error}</div>}
        <div className="space-y-1">
          {memories.map((m) => (
            <button key={m.name} onClick={() => { setSelected(m.name); setEditing(null) }}
              className={`w-full text-left px-3 py-2 rounded-lg transition ${
                current?.name === m.name ? 'bg-mint-50 border border-mint-200' : 'hover:bg-mint-50/50 border border-transparent'
              }`}>
              <div className="text-sm font-mono font-semibold text-slate-700">{m.name}</div>
              <div className="text-xs text-slate-400 truncate">{(m.content || '').slice(0, 60) || '（空）'}</div>
            </button>
          ))}
          {memories.length === 0 && !error && (
            <div className="text-xs text-slate-400 px-1 py-4 text-center">核心记忆为空——共识通过后会出现在这里</div>
          )}
        </div>
      </div>

      <div className="lg:col-span-2 bg-white rounded-2xl border border-mint-100 shadow-mint-card p-5">
        {creating ? (
          <>
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">新建核心记忆</div>
            <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="记忆名（如 proj-pref）"
              className="w-full px-2 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400 mb-2" />
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={8} placeholder="内容（Markdown）"
              className="w-full px-2 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
            <div className="flex justify-end gap-2 mt-2">
              <button onClick={() => setCreating(false)} className="px-3 py-1.5 text-xs rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200">取消</button>
              <button disabled={busy || !newName.trim()} onClick={() => save(newName.trim(), draft)}
                className="px-4 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">保存</button>
            </div>
          </>
        ) : current ? (
          <>
            <div className="flex items-center justify-between mb-3">
              <span className="font-mono font-bold text-slate-800">{current.name}</span>
              <div className="flex gap-2">
                {editing === current.name ? (
                  <>
                    <button onClick={() => setEditing(null)} className="px-3 py-1.5 text-xs rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200">取消</button>
                    <button disabled={busy} onClick={() => save(current.name, draft)}
                      className="px-4 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">保存</button>
                  </>
                ) : (
                  <>
                    <button onClick={() => { setEditing(current.name); setDraft(current.content || '') }}
                      className="px-3 py-1.5 text-xs rounded-lg border border-mint-200 text-mint-700 hover:bg-mint-50 font-medium">编辑</button>
                    <button disabled={busy} onClick={() => remove(current.name)}
                      className="px-3 py-1.5 text-xs rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-40">删除</button>
                  </>
                )}
              </div>
            </div>
            {msg && <div className="text-xs text-mint-700 mb-2">{msg}</div>}
            {editing === current.name ? (
              <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={12}
                className="w-full px-2.5 py-2 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
            ) : (
              <pre className="bg-mint-50/40 border border-mint-100 rounded-xl p-3 text-sm whitespace-pre-wrap min-h-32">{current.content || '（空）'}</pre>
            )}
          </>
        ) : (
          <div className="text-sm text-slate-400 py-10 text-center">选择或新建一条核心记忆</div>
        )}
      </div>
    </div>
  )
}

/* ---------------- Tab 2 任务上下文 ---------------- */

function ContextTab() {
  const { data: tasks } = usePolling<TaskRecord[]>(() => api.tasks(), POLL.tasksSlow)
  const [taskId, setTaskId] = useState('')
  const [notes, setNotes] = useState<Record<string, string | null> | null>(null)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [newKey, setNewKey] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  // 派生列表仅随 tasks 变化重算，不随每次渲染（编辑态/加载态触发）重复排序
  const recent = useMemo(
    () => (tasks ?? [])
      .slice().sort((a, b) => Number(b.created_at) - Number(a.created_at)).slice(0, 30),
    [tasks],
  )

  const load = async (id: string) => {
    setTaskId(id)
    setNotes(null)
    setEditingKey(null)
    setMsg(null)
    if (!id) return
    try {
      setNotes((await api.taskContext(id)).notes)
    } catch (e) {
      setMsg(`加载失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const saveNote = async (key: string, content: string) => {
    try {
      await api.taskContextPut(taskId, key, content)
      setEditingKey(null)
      setMsg(`已写入笔记 ${key}`)
      await load(taskId)
    } catch (e) {
      setMsg(`写入失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const archive = async () => {
    if (!window.confirm(`归档清理任务 [${taskId}] 的全部上下文笔记？此操作不可恢复。`)) return
    try {
      await api.taskContextArchive(taskId)
      setMsg('已归档清理')
      await load(taskId)
    } catch (e) {
      setMsg(`归档失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-sm p-3 flex flex-wrap items-center gap-2">
        <select value={taskId} onChange={(e) => load(e.target.value)}
          className="px-2.5 py-1.5 text-sm rounded-lg border border-mint-200/70 bg-white focus:outline-none focus:ring-2 focus:ring-mint-400">
          <option value="">选择任务…</option>
          {recent.map((t) => (
            <option key={t.task_id} value={t.task_id}>
              {t.task_id} · {t.status} · {t.prompt.slice(0, 30)}
            </option>
          ))}
        </select>
        {taskId && notes && Object.keys(notes).length > 0 && (
          <button onClick={archive} className="ml-auto px-3 py-1.5 text-xs rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50">
            归档清理全部笔记
          </button>
        )}
        {msg && <span className="text-xs text-mint-700">{msg}</span>}
      </div>

      {!taskId && (
        <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-10 text-center text-sm text-slate-400">
          选择一个任务查看其上下文记忆（含任务终态自动写入的 result 笔记）
        </div>
      )}
      {taskId && notes && (
        <div className="space-y-3">
          {Object.entries(notes).map(([k, v]) => (
            <div key={k} className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-sm font-semibold text-slate-700">{k}</span>
                {editingKey === k ? (
                  <button disabled={!taskId} onClick={() => saveNote(k, draft)}
                    className="px-3 py-1 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold">保存</button>
                ) : (
                  <button onClick={() => { setEditingKey(k); setDraft(v || '') }}
                    className="px-3 py-1 text-xs rounded-lg border border-mint-200 text-mint-700 hover:bg-mint-50">编辑</button>
                )}
              </div>
              {editingKey === k ? (
                <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={6}
                  className="w-full px-2.5 py-2 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
              ) : (
                <pre className="bg-mint-50/40 border border-mint-100 rounded-xl p-3 text-xs whitespace-pre-wrap max-h-60 overflow-auto">{v || '（空）'}</pre>
              )}
            </div>
          ))}
          {Object.keys(notes).length === 0 && (
            <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-8 text-center text-sm text-slate-400">
              该任务暂无上下文笔记
            </div>
          )}
          {/* 新增笔记 */}
          <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-4">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">新增笔记</div>
            <div className="flex gap-2">
              <input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="笔记名（如 decision）"
                className="w-48 px-2 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
              <button disabled={!newKey.trim()} onClick={() => { saveNote(newKey.trim(), ''); setNewKey('') }}
                className="px-4 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">创建</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* ---------------- Tab 3 经验库 ---------------- */

function ExperienceTab() {
  const [q, setQ] = useState('')
  const [successfulOnly, setSuccessfulOnly] = useState(true)
  const [results, setResults] = useState<ExperienceRecord[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const search = async () => {
    setSearching(true)
    setMsg(null)
    try {
      setResults(await api.searchExperiences({ query: q || ' ', successful_only: successfulOnly, limit: 30 }))
    } catch (e) {
      setMsg(`检索失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSearching(false)
    }
  }

  const remove = async (id: string) => {
    if (!window.confirm(`确定删除经验 [${id}] 吗？`)) return
    try {
      await api.deleteExperience(id)
      setResults((prev) => prev?.filter((r) => r.exp_id !== id) ?? null)
      setMsg(`已删除 ${id}`)
    } catch (e) {
      setMsg(`删除失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const reindex = async () => {
    setMsg(null)
    try {
      const r = await api.reindexExperiences()
      setMsg(`重建向量索引完成：${r.reindexed} 条`)
    } catch (e) {
      setMsg(`重建失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const list = results ?? []
  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-sm p-3 flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()}
          placeholder="混合检索（关键词 + 语义）历史任务经验…"
          className="flex-1 min-w-56 px-3 py-1.5 text-sm rounded-lg border border-mint-200/70 focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <label className="flex items-center gap-1.5 text-xs text-slate-600 cursor-pointer select-none">
          <input type="checkbox" checked={successfulOnly} onChange={(e) => setSuccessfulOnly(e.target.checked)} className="accent-mint-600" />
          仅成功
        </label>
        <button onClick={search} disabled={searching}
          className="px-4 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">
          {searching ? '检索中…' : '检索'}
        </button>
        <button onClick={reindex} className="px-3 py-1.5 text-xs rounded-lg border border-mint-200 text-mint-700 hover:bg-mint-50 font-medium">
          重建向量索引
        </button>
        {msg && <span className="text-xs text-mint-700 w-full">{msg}</span>}
      </div>

      {results === null ? (
        <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-10 text-center text-sm text-slate-400">
          输入关键词检索历史经验（每次派发时系统会自动注入最相关的 3 条成功经验）
        </div>
      ) : list.length === 0 ? (
        <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-10 text-center text-sm text-slate-400">没有匹配的经验</div>
      ) : (
        <div className="space-y-2">
          {list.map((r) => (
            <div key={r.exp_id} className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-4">
              <button onClick={() => setExpanded(expanded === r.exp_id ? null : r.exp_id)}
                className="w-full text-left flex items-center gap-2.5">
                <span className={`w-2 h-2 rounded-full shrink-0 ${r.success ? 'bg-mint-500' : 'bg-rose-500'}`} />
                <StatusBadge status={r.success ? 'done' : 'failed'} />
                <span className="font-mono text-xs text-slate-500 shrink-0">{r.exp_id}</span>
                <span className="text-sm text-slate-700 truncate flex-1">{r.prompt}</span>
                <span className="text-xs text-slate-400 shrink-0">命中 {r.hits ?? 0} 次</span>
                <span className="text-xs text-slate-400 shrink-0">v{r.version ?? 1}</span>
              </button>
              {expanded === r.exp_id && (
                <div className="mt-3 border-t border-slate-50 pt-3 space-y-2">
                  <div className="text-xs text-slate-500"><span className="font-bold">产出摘要：</span>{r.output_summary || '（无）'}</div>
                  {r.verdict && (
                    <div className="text-xs text-slate-500">
                      <span className="font-bold">安全裁决：</span>{r.verdict.verdict} — {r.verdict.reason}
                    </div>
                  )}
                  <div className="text-xs text-slate-400 flex gap-4">
                    <span>role: {r.role}</span>
                    <span>记录于 {timeAgo(r.created_at)}</span>
                    {r.artifacts.length > 0 && <span>产物 {r.artifacts.length} 个</span>}
                  </div>
                  <button onClick={() => remove(r.exp_id)}
                    className="px-3 py-1 text-xs rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50">删除</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ---------------- Tab 4 演化提案 ---------------- */

function ProposalTab() {
  const reflection = usePolling(() => api.reflection(50), POLL.reflection)
  const proposals = usePolling(() => api.proposals(), POLL.services)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [voter, setVoter] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const vote = async (id: string, verdict: 'pass' | 'block') => {
    setBusy(true)
    setMsg(null)
    try {
      const v = voter[id] || 'brain'
      const r = await api.voteProposal(id, v, verdict)
      setMsg(`已投 ${v}/${verdict} → 提案 ${r.status === 'applied' ? '三票通过并已应用' : `状态 ${r.status}`}`)
      proposals.refresh()
    } catch (e) {
      setMsg(`投票失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    if (!window.confirm(`丢弃提案 [${id}] 吗？`)) return
    try {
      await api.deleteProposal(id)
      proposals.refresh()
    } catch (e) {
      setMsg(`删除失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const refl = reflection.data
  const list = proposals.data ?? []
  const pending = list.filter((p) => p.status === 'pending')

  return (
    <div className="space-y-4">
      {/* 反思报告（证据面板） */}
      <div className="bg-gradient-to-r from-mint-50/80 via-white to-white border border-mint-200/70 rounded-2xl p-5 shadow-mint-sm">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-bold text-slate-800">反思报告（近 {refl?.window ?? 50} 条经验）</h3>
          <button onClick={() => reflection.refresh()} className="text-xs text-mint-700 hover:underline font-medium">刷新</button>
        </div>
        {refl ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <Stat label="经验总量" value={String(refl.total)} />
            <Stat label="成功率" value={refl.total > 0 ? `${Math.round((refl.success / refl.total) * 100)}%` : '—'} />
            <Stat label="失败任务" value={String(refl.failed_recent.length)} />
            <Stat label="高频命中" value={String(refl.most_hit.length)} />
          </div>
        ) : (
          <div className="text-xs text-slate-400">加载反思报告中…</div>
        )}
        {refl && refl.failed_recent.length > 0 && (
          <div className="mt-3 text-xs text-slate-500">
            <span className="font-bold text-rose-600">近期失败：</span>
            {refl.failed_recent.slice(0, 3).map((f) => (
              <span key={f.exp_id} className="mr-3" title={f.prompt}>{f.exp_id} · {f.prompt.slice(0, 26)}…</span>
            ))}
          </div>
        )}
        <div className="mt-2 text-[11px] text-slate-400">修改技能定义请走提案流程（证据 → 提案 → 三票共识 → 自动应用），不要直接改文件。</div>
      </div>

      {msg && <div className="text-xs text-mint-700 bg-mint-50 border border-mint-200 rounded-xl px-3 py-2">{msg}</div>}

      {/* 提案列表 */}
      {list.length === 0 ? (
        <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-10 text-center text-sm text-slate-400">
          暂无演化提案——由大脑在 G5 复盘时依据反思报告起草
        </div>
      ) : (
        <div className="space-y-3">
          {list.map((p) => (
            <ProposalCard
              key={p.proposal_id} proposal={p}
              expanded={expanded === p.proposal_id}
              onToggle={() => setExpanded(expanded === p.proposal_id ? null : p.proposal_id)}
              voter={voter[p.proposal_id] ?? 'brain'}
              onVoterChange={(v) => setVoter((prev) => ({ ...prev, [p.proposal_id]: v }))}
              onVote={(verdict) => vote(p.proposal_id, verdict)}
              onDelete={() => remove(p.proposal_id)}
              busy={busy} pendingCount={pending.length}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white/80 border border-mint-100 rounded-xl p-2.5">
      <div className="text-slate-400">{label}</div>
      <div className="text-lg font-bold text-slate-800 mt-0.5">{value}</div>
    </div>
  )
}

function ProposalCard({
  proposal: p, expanded, onToggle, voter, onVoterChange, onVote, onDelete, busy, pendingCount,
}: {
  proposal: ProposalRecord
  expanded: boolean
  onToggle: () => void
  voter: string
  onVoterChange: (v: string) => void
  onVote: (verdict: 'pass' | 'block') => void
  onDelete: () => void
  busy: boolean
  pendingCount: number
}) {
  const statusCls = p.status === 'applied' ? 'bg-mint-100 text-mint-700'
    : p.status === 'rejected' ? 'bg-rose-100 text-rose-700'
    : 'bg-amber-100 text-amber-700'
  return (
    <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-4">
      <button onClick={onToggle} className="w-full text-left">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${statusCls}`}>{p.status}</span>
          <span className="font-mono text-xs text-slate-500">{p.proposal_id}</span>
          <span className="text-sm font-semibold text-slate-700 font-mono truncate flex-1" title={p.target_path}>{p.target_path}</span>
          <span className="text-xs text-slate-400">{timeAgo(p.created_at)}</span>
        </div>
        <p className="text-xs text-slate-500 mt-1.5 truncate">{p.reason}</p>
      </button>

      {/* 投票进度 */}
      <div className="mt-2 flex items-center gap-2 text-xs flex-wrap">
        {(['security', 'brain', 'executor'] as const).map((v) => {
          const vote = p.votes[v]
          const cls = !vote ? 'bg-slate-100 text-slate-400'
            : vote.verdict === 'pass' ? 'bg-mint-50 text-mint-700 border border-mint-200'
            : 'bg-rose-50 text-rose-700 border border-rose-200'
          return (
            <span key={v} className={`px-2 py-0.5 rounded-full ${cls}`}>
              {v}: {vote ? (vote.verdict === 'pass' ? '✓ pass' : '✕ block') : '等待'}
            </span>
          )
        })}
        {p.status === 'rejected' && p.reject_reason && (
          <span className="text-rose-600 truncate" title={p.reject_reason}>否决: {p.reject_reason}</span>
        )}
      </div>

      {expanded && (
        <div className="mt-3 border-t border-slate-50 pt-3 space-y-3">
          {/* Diff 并排：原内容微浅红 / 修订内容淡薄荷 */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <div className="text-[11px] font-bold text-rose-500 uppercase tracking-wider mb-1">原内容</div>
              <pre className="bg-rose-50/70 border border-rose-100 rounded-xl p-2.5 text-xs whitespace-pre-wrap max-h-72 overflow-auto">{p.original_content || '（新文件）'}</pre>
            </div>
            <div>
              <div className="text-[11px] font-bold text-mint-600 uppercase tracking-wider mb-1">修订内容</div>
              <pre className="bg-mint-50/70 border border-mint-100 rounded-xl p-2.5 text-xs whitespace-pre-wrap max-h-72 overflow-auto">{p.content}</pre>
            </div>
          </div>
          {p.evidence_exp_ids.length > 0 && (
            <div className="text-xs text-slate-500">证据经验: {p.evidence_exp_ids.join(', ')}</div>
          )}
          {p.status === 'pending' ? (
            <div className="flex items-center gap-2 flex-wrap">
              <select value={voter} onChange={(e) => onVoterChange(e.target.value)}
                className="px-2 py-1 text-xs rounded-lg border border-mint-200/70">
                <option value="brain">以 brain 视角</option>
                <option value="executor">以 executor 视角</option>
              </select>
              <button disabled={busy} onClick={() => onVote('pass')}
                className="px-4 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">投 Pass</button>
              <button disabled={busy} onClick={() => onVote('block')}
                className="px-4 py-1.5 text-xs rounded-lg bg-rose-600 text-white hover:bg-rose-500 font-semibold disabled:opacity-40">投 Block</button>
              <span className="text-[11px] text-slate-400">三票全 pass 自动应用；security 已由规则引擎自动裁决（{pendingCount} 个提案待决）</span>
            </div>
          ) : (
            <button onClick={onDelete} className="px-3 py-1 text-xs rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50">丢弃提案记录</button>
          )}
        </div>
      )}
    </div>
  )
}
