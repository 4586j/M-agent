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
      <div className="inline-flex p-1 bg-zinc-950/80 border border-white/[0.08] rounded-2xl mb-6 shadow-inner">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={`btn-press px-4 py-2 text-xs rounded-xl font-medium transition duration-150 ${
              tab === t.key
                ? 'bg-zinc-800 text-emerald-400 shadow-sm border border-white/[0.08] font-semibold'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50'
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
      <section className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5">
        <h3 className="text-sm font-semibold text-zinc-100 mb-1">上传文档</h3>
        <p className="text-xs text-zinc-400 mb-3">
          项目规范 / API 文档 / 运维手册等确定性资料。派发时自动检索相关片段注入任务上下文（空库零影响）。
        </p>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="文档标题"
          className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 mb-2.5 font-mono"
        />
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={8}
          placeholder="粘贴文档正文（Markdown/纯文本均可，按段落自动切块）"
          className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 font-mono resize-y"
        />
        <div className="flex items-center gap-3 mt-3">
          <button
            type="button"
            onClick={upload}
            disabled={busy || !content.trim()}
            className="px-5 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
          >
            {busy ? '上传中…' : '上传文档'}
          </button>
          {msg && <span className="text-xs text-emerald-400 font-mono">{msg}</span>}
        </div>
      </section>

      <section className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5">
        <h3 className="text-sm font-semibold text-zinc-100 mb-3">文档（{docs.length}）与检索预览</h3>
        <div className="flex items-center gap-2 mb-3">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入查询，预览将被注入的片段…"
            className="flex-1 px-3 py-1.5 text-xs border border-white/[0.08] rounded-xl bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
          />
          <button
            type="button"
            onClick={search}
            disabled={searching || !query.trim()}
            className="px-4 py-1.5 text-xs rounded-xl bg-indigo-500/20 text-indigo-300 hover:bg-indigo-500/30 border border-indigo-500/30 font-medium disabled:opacity-40 transition"
          >
            {searching ? '检索中…' : '检索'}
          </button>
        </div>
        {results && (
          <div className="space-y-2 mb-3 max-h-52 overflow-y-auto">
            {results.length === 0 && (
              <div className="text-xs text-zinc-500">没有命中片段</div>
            )}
            {results.map((r, i) => (
              <div key={`${r.doc_id}-${i}`} className="border border-white/[0.06] rounded-xl px-3 py-2 bg-zinc-950/80">
                <div className="text-xs font-medium text-emerald-400 font-mono">{r.title}</div>
                <div className="text-xs text-zinc-400 line-clamp-3 mt-1">{r.snippet}</div>
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-xl p-3 mb-2">
            加载失败：{error}
          </div>
        )}
        {docs.length === 0 && !error ? (
          <div className="text-zinc-500 text-sm py-8 text-center">知识库为空——上传第一份文档</div>
        ) : (
          <div className="space-y-2 max-h-72 overflow-y-auto">
            {docs.map((d) => (
              <div key={d.doc_id} className="flex items-center gap-3 border border-white/[0.06] bg-zinc-950/50 hover:bg-zinc-800/40 rounded-xl p-2.5 transition">
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-zinc-200 truncate">{d.title}</div>
                  <div className="text-[11px] text-zinc-500 font-mono">
                    {d.chunk_count} 块 · {d.content_chars} 字 · 更新于 {timeAgo(d.updated_at)}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => { if (window.confirm(`删除文档「${d.title}」？`)) remove(d.doc_id, d.title) }}
                  className="px-2.5 py-1 text-xs text-zinc-400 hover:text-rose-400 transition"
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
      <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-3.5">
        <div className="flex items-center justify-between px-1 mb-2.5">
          <span className="text-xs font-bold text-zinc-400 uppercase tracking-wider">记忆列表</span>
          <button
            type="button"
            onClick={() => { setCreating(true); setEditing(null); setNewName('') }}
            className="text-xs text-emerald-400 hover:text-emerald-300 hover:underline font-medium"
          >
            + 新建
          </button>
        </div>
        {error && <div className="text-xs text-rose-400 px-1">{error}</div>}
        <div className="space-y-1">
          {memories.map((m) => (
            <button
              key={m.name}
              type="button"
              onClick={() => { setSelected(m.name); setEditing(null) }}
              className={`w-full text-left px-3 py-2 rounded-xl transition duration-150 ${
                current?.name === m.name
                  ? 'bg-emerald-500/15 border border-emerald-500/30'
                  : 'hover:bg-zinc-800/40 border border-transparent'
              }`}
            >
              <div className="text-sm font-mono font-semibold text-zinc-200">{m.name}</div>
              <div className="text-xs text-zinc-400 truncate">{(m.content || '').slice(0, 60) || '（空）'}</div>
            </button>
          ))}
          {memories.length === 0 && !error && (
            <div className="text-xs text-zinc-500 px-1 py-6 text-center">核心记忆为空——共识通过后会出现在这里</div>
          )}
        </div>
      </div>

      <div className="lg:col-span-2 bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5">
        {creating ? (
          <>
            <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider mb-2.5">新建核心记忆</div>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="记忆名（如 proj-pref）"
              className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 mb-2.5"
            />
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={8}
              placeholder="内容（Markdown）"
              className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
            />
            <div className="flex justify-end gap-2 mt-3">
              <button
                type="button"
                onClick={() => setCreating(false)}
                className="px-3 py-1.5 text-xs rounded-xl bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition"
              >
                取消
              </button>
              <button
                type="button"
                disabled={busy || !newName.trim()}
                onClick={() => save(newName.trim(), draft)}
                className="px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
              >
                保存
              </button>
            </div>
          </>
        ) : current ? (
          <>
            <div className="flex items-center justify-between mb-3">
              <span className="font-mono font-bold text-zinc-100 text-base">{current.name}</span>
              <div className="flex gap-2">
                {editing === current.name ? (
                  <>
                    <button
                      type="button"
                      onClick={() => setEditing(null)}
                      className="px-3 py-1.5 text-xs rounded-xl bg-zinc-800 text-zinc-400 hover:bg-zinc-700 transition"
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => save(current.name, draft)}
                      className="px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
                    >
                      保存
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => { setEditing(current.name); setDraft(current.content || '') }}
                      className="px-3 py-1.5 text-xs rounded-xl border border-white/[0.08] bg-zinc-950/70 text-zinc-300 hover:bg-zinc-800 font-medium transition"
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => remove(current.name)}
                      className="px-3 py-1.5 text-xs rounded-xl border border-rose-500/20 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 disabled:opacity-40 transition"
                    >
                      删除
                    </button>
                  </>
                )}
              </div>
            </div>
            {msg && <div className="text-xs text-emerald-400 mb-2 font-mono">{msg}</div>}
            {editing === current.name ? (
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={12}
                className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
              />
            ) : (
              <pre className="bg-[#050507] border border-white/[0.07] rounded-xl p-3.5 text-xs text-zinc-200 font-mono whitespace-pre-wrap min-h-32">
                {current.content || '（空）'}
              </pre>
            )}
          </>
        ) : (
          <div className="text-sm text-zinc-500 py-12 text-center">选择或新建一条核心记忆</div>
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
      <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-3.5 flex flex-wrap items-center gap-2.5">
        <select
          value={taskId}
          onChange={(e) => load(e.target.value)}
          className="px-3 py-1.5 text-xs rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-200 focus:outline-none focus:border-emerald-500/40 font-mono"
        >
          <option value="">选择任务…</option>
          {recent.map((t) => (
            <option key={t.task_id} value={t.task_id}>
              {t.task_id} · {t.status} · {t.prompt.slice(0, 30)}
            </option>
          ))}
        </select>
        {taskId && notes && Object.keys(notes).length > 0 && (
          <button
            type="button"
            onClick={archive}
            className="ml-auto px-3 py-1.5 text-xs rounded-xl border border-rose-500/20 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 transition"
          >
            归档清理全部笔记
          </button>
        )}
        {msg && <span className="text-xs text-emerald-400 font-mono">{msg}</span>}
      </div>

      {!taskId && (
        <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-12 text-center text-sm text-zinc-500">
          选择一个任务查看其上下文记忆（含任务终态自动写入的 result 笔记）
        </div>
      )}

      {taskId && notes && (
        <div className="space-y-3">
          {Object.entries(notes).map(([k, v]) => (
            <div key={k} className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-xs font-semibold text-zinc-200">{k}</span>
                {editingKey === k ? (
                  <button
                    type="button"
                    disabled={!taskId}
                    onClick={() => saveNote(k, draft)}
                    className="px-3 py-1 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold transition"
                  >
                    保存
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => { setEditingKey(k); setDraft(v || '') }}
                    className="px-3 py-1 text-xs rounded-xl border border-white/[0.08] bg-zinc-950/70 text-zinc-300 hover:bg-zinc-800 transition"
                  >
                    编辑
                  </button>
                )}
              </div>
              {editingKey === k ? (
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={6}
                  className="w-full px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 resize-y"
                />
              ) : (
                <pre className="bg-[#050507] border border-white/[0.07] rounded-xl p-3 text-xs font-mono text-zinc-300 whitespace-pre-wrap max-h-60 overflow-auto">
                  {v || '（空）'}
                </pre>
              )}
            </div>
          ))}

          {Object.keys(notes).length === 0 && (
            <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-8 text-center text-sm text-zinc-500">
              该任务暂无上下文笔记
            </div>
          )}

          {/* 新增笔记 */}
          <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-4">
            <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider mb-2">新增笔记</div>
            <div className="flex gap-2">
              <input
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder="笔记名（如 decision）"
                className="w-48 px-3 py-1.5 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
              />
              <button
                type="button"
                disabled={!newKey.trim()}
                onClick={() => { saveNote(newKey.trim(), ''); setNewKey('') }}
                className="px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
              >
                创建
              </button>
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
      <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-3.5 flex flex-wrap items-center gap-2.5">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && search()}
          placeholder="混合检索（关键词 + 语义）历史任务经验…"
          className="flex-1 min-w-56 px-3 py-1.5 text-xs rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40 font-mono"
        />
        <label className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={successfulOnly}
            onChange={(e) => setSuccessfulOnly(e.target.checked)}
            className="accent-emerald-500"
          />
          仅成功
        </label>
        <button
          type="button"
          onClick={search}
          disabled={searching}
          className="px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
        >
          {searching ? '检索中…' : '检索'}
        </button>
        <button
          type="button"
          onClick={reindex}
          className="px-3 py-1.5 text-xs rounded-xl border border-white/[0.08] bg-zinc-950/70 text-zinc-300 hover:bg-zinc-800 font-medium transition"
        >
          重建向量索引
        </button>
        {msg && <span className="text-xs text-emerald-400 w-full font-mono">{msg}</span>}
      </div>

      {results === null ? (
        <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-12 text-center text-sm text-zinc-500">
          输入关键词检索历史经验（每次派发时系统会自动注入最相关的 3 条成功经验）
        </div>
      ) : list.length === 0 ? (
        <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-12 text-center text-sm text-zinc-500">
          没有匹配的经验
        </div>
      ) : (
        <div className="space-y-2.5">
          {list.map((r) => (
            <div
              key={r.exp_id}
              className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-4 hover:border-emerald-500/30 transition duration-150"
            >
              <button
                type="button"
                onClick={() => setExpanded(expanded === r.exp_id ? null : r.exp_id)}
                className="w-full text-left flex items-center gap-2.5"
              >
                <span className={`w-2 h-2 rounded-full shrink-0 ${r.success ? 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]' : 'bg-rose-500'}`} />
                <StatusBadge status={r.success ? 'done' : 'failed'} />
                <span className="font-mono text-xs text-zinc-400 shrink-0">{r.exp_id}</span>
                <span className="text-sm text-zinc-200 truncate flex-1 font-sans">{r.prompt}</span>
                <span className="text-xs text-zinc-500 shrink-0 font-mono">命中 {r.hits ?? 0} 次</span>
                <span className="text-xs text-zinc-500 shrink-0 font-mono">v{r.version ?? 1}</span>
              </button>
              {expanded === r.exp_id && (
                <div className="mt-3 border-t border-white/[0.06] pt-3 space-y-2">
                  <div className="text-xs text-zinc-300"><span className="text-zinc-500">产出摘要：</span>{r.output_summary || '（无）'}</div>
                  {r.verdict && (
                    <div className="text-xs text-zinc-400">
                      <span className="text-zinc-500">安全裁决：</span>{r.verdict.verdict} — {r.verdict.reason}
                    </div>
                  )}
                  <div className="text-xs text-zinc-500 flex gap-4 font-mono">
                    <span>role: {r.role}</span>
                    <span>记录于 {timeAgo(r.created_at)}</span>
                    {r.artifacts.length > 0 && <span>产物 {r.artifacts.length} 个</span>}
                  </div>
                  <button
                    type="button"
                    onClick={() => remove(r.exp_id)}
                    className="px-3 py-1 text-xs rounded-xl border border-rose-500/20 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 transition"
                  >
                    删除
                  </button>
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
      <div className="bg-zinc-900/40 backdrop-blur-xl border border-white/[0.07] rounded-2xl p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
            <h3 className="text-sm font-semibold text-zinc-100">反思报告（近 {refl?.window ?? 50} 条经验）</h3>
          </div>
          <button
            type="button"
            onClick={() => reflection.refresh()}
            className="text-xs text-emerald-400 hover:text-emerald-300 hover:underline font-medium"
          >
            刷新
          </button>
        </div>
        {refl ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <Stat label="经验总量" value={String(refl.total)} />
            <Stat label="成功率" value={refl.total > 0 ? `${Math.round((refl.success / refl.total) * 100)}%` : '—'} />
            <Stat label="失败任务" value={String(refl.failed_recent.length)} />
            <Stat label="高频命中" value={String(refl.most_hit.length)} />
          </div>
        ) : (
          <div className="text-xs text-zinc-500">加载反思报告中…</div>
        )}
        {refl && refl.failed_recent.length > 0 && (
          <div className="mt-3 text-xs text-zinc-400 font-mono">
            <span className="font-bold text-rose-400">近期失败：</span>
            {refl.failed_recent.slice(0, 3).map((f) => (
              <span key={f.exp_id} className="mr-3" title={f.prompt}>{f.exp_id} · {f.prompt.slice(0, 26)}…</span>
            ))}
          </div>
        )}
        <div className="mt-2 text-[11px] text-zinc-500">修改技能定义请走提案流程（证据 → 提案 → 三票共识 → 自动应用），不要直接改文件。</div>
      </div>

      {msg && (
        <div className="text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-2 font-mono">
          {msg}
        </div>
      )}

      {/* 提案列表 */}
      {list.length === 0 ? (
        <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] p-12 text-center text-sm text-zinc-500">
          暂无演化提案——由大脑在 G5 复盘时依据反思报告起草
        </div>
      ) : (
        <div className="space-y-3">
          {list.map((p) => (
            <ProposalCard
              key={p.proposal_id}
              proposal={p}
              expanded={expanded === p.proposal_id}
              onToggle={() => setExpanded(expanded === p.proposal_id ? null : p.proposal_id)}
              voter={voter[p.proposal_id] ?? 'brain'}
              onVoterChange={(v) => setVoter((prev) => ({ ...prev, [p.proposal_id]: v }))}
              onVote={(verdict) => vote(p.proposal_id, verdict)}
              onDelete={() => remove(p.proposal_id)}
              busy={busy}
              pendingCount={pending.length}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-zinc-950/60 border border-white/[0.06] rounded-xl p-3">
      <div className="text-zinc-500 text-xs">{label}</div>
      <div className="text-xl font-bold font-mono tracking-tight text-zinc-100 mt-1">{value}</div>
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
  const statusCls = p.status === 'applied' ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
    : p.status === 'rejected' ? 'bg-rose-500/15 text-rose-300 border border-rose-500/30'
    : 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
  return (
    <div className="bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-4">
      <button type="button" onClick={onToggle} className="w-full text-left">
        <div className="flex items-center gap-2.5 flex-wrap">
          <span className={`px-2 py-0.5 rounded text-xs font-bold font-mono ${statusCls}`}>{p.status}</span>
          <span className="font-mono text-xs text-zinc-400">{p.proposal_id}</span>
          <span className="text-sm font-semibold text-zinc-200 font-mono truncate flex-1" title={p.target_path}>{p.target_path}</span>
          <span className="text-xs text-zinc-500 font-mono">{timeAgo(p.created_at)}</span>
        </div>
        <p className="text-xs text-zinc-400 mt-1.5 truncate">{p.reason}</p>
      </button>

      {/* 投票进度 */}
      <div className="mt-2.5 flex items-center gap-2 text-xs flex-wrap font-mono">
        {(['security', 'brain', 'executor'] as const).map((v) => {
          const vote = p.votes[v]
          const cls = !vote ? 'bg-zinc-950 text-zinc-500 border border-white/[0.05]'
            : vote.verdict === 'pass' ? 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/20'
            : 'bg-rose-500/10 text-rose-300 border border-rose-500/20'
          return (
            <span key={v} className={`px-2 py-0.5 rounded-full ${cls}`}>
              {v}: {vote ? (vote.verdict === 'pass' ? '✓ pass' : '✕ block') : '等待'}
            </span>
          )
        })}
        {p.status === 'rejected' && p.reject_reason && (
          <span className="text-rose-400 truncate" title={p.reject_reason}>否决: {p.reject_reason}</span>
        )}
      </div>

      {expanded && (
        <div className="mt-3 border-t border-white/[0.06] pt-3 space-y-3">
          {/* Diff 并排：原内容暗红 / 修订内容暗薄荷 */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <div className="text-[11px] font-bold text-rose-400 uppercase tracking-wider mb-1.5 font-mono">原内容</div>
              <pre className="bg-rose-950/20 border border-rose-500/20 text-rose-300 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap max-h-72 overflow-auto">
                {p.original_content || '（新文件）'}
              </pre>
            </div>
            <div>
              <div className="text-[11px] font-bold text-emerald-400 uppercase tracking-wider mb-1.5 font-mono">修订内容</div>
              <pre className="bg-emerald-950/20 border border-emerald-500/20 text-emerald-300 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap max-h-72 overflow-auto">
                {p.content}
              </pre>
            </div>
          </div>
          {p.evidence_exp_ids.length > 0 && (
            <div className="text-xs text-zinc-400 font-mono">证据经验: {p.evidence_exp_ids.join(', ')}</div>
          )}
          {p.status === 'pending' ? (
            <div className="flex items-center gap-2.5 flex-wrap pt-1">
              <select
                value={voter}
                onChange={(e) => onVoterChange(e.target.value)}
                className="px-2.5 py-1 text-xs rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-200"
              >
                <option value="brain">以 brain 视角</option>
                <option value="executor">以 executor 视角</option>
              </select>
              <button
                type="button"
                disabled={busy}
                onClick={() => onVote('pass')}
                className="px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
              >
                投 Pass
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => onVote('block')}
                className="px-4 py-1.5 text-xs rounded-xl bg-rose-500 hover:bg-rose-400 text-white font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(244,63,94,0.3)]"
              >
                投 Block
              </button>
              <span className="text-[11px] text-zinc-500">
                三票全 pass 自动应用；security 已由规则引擎自动裁决（{pendingCount} 个提案待决）
              </span>
            </div>
          ) : (
            <button
              type="button"
              onClick={onDelete}
              className="px-3 py-1.5 text-xs rounded-xl border border-white/[0.08] bg-zinc-950 text-zinc-400 hover:bg-zinc-800 transition"
            >
              丢弃提案记录
            </button>
          )}
        </div>
      )}
    </div>
  )
}
