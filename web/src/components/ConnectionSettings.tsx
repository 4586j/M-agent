import { useState } from 'react'
import {
  type Connection,
  useActiveConnectionId,
  getConnections,
  activate,
  createConnection,
  saveConnection as persistConnection,
  deleteConnection,
  testConnection,
} from '../api/connections'
import { getOperator } from '../api/client'

const LS_OPERATOR = 'orch_operator'

/** 操作者名称输入：随写操作以 X-Operator 头发送，审计/审批记录落操作者归属 */
function OperatorField() {
  const [name, setName] = useState(getOperator())
  const [saved, setSaved] = useState(false)
  const save = () => {
    try {
      const v = name.trim()
      if (v) localStorage.setItem(LS_OPERATOR, v)
      else localStorage.removeItem(LS_OPERATOR)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1500)
    } catch { /* 隐私模式等存储不可用：静默 */ }
  }
  return (
    <div className="border-t border-white/[0.07] mt-3 pt-3">
      <div className="flex items-center gap-1.5">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={save}
          placeholder="操作者（审计/审批归属，如 zhang）"
          className="flex-1 min-w-0 px-2.5 py-1.5 text-xs bg-zinc-900 border border-white/[0.08] text-zinc-200 placeholder-zinc-500 rounded-lg focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
        />
        {saved && <span className="text-xs text-emerald-400 shrink-0 font-mono">已保存</span>}
      </div>
    </div>
  )
}

/**
 * 连接管理器（顶栏下拉）：保存多个 brain 连接（名称 + 地址 + API Key），
 * 点击切换活动连接；增删改 + 测试连接。
 *
 * - baseUrl 留空 = 相对路径（dev 走 vite proxy / prod 同源直调，单 brain 部署原样）
 * - 测试连接用裸 fetch 区分 200 / 401（Key 错）/ 403（非回环未配 Key）/ 网络不可达
 */
export default function ConnectionSettings() {
  const [open, setOpen] = useState(false)
  const activeId = useActiveConnectionId()
  const conns = getConnections()
  const active = conns.find((c) => c.id === activeId) ?? conns[0]
  const hasKey = (active?.apiKey ?? '') !== ''

  // 编辑/新增草稿；null = 未在编辑
  const [draft, setDraft] = useState<Connection | null>(null)
  const [isNew, setIsNew] = useState(false)
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [testing, setTesting] = useState(false)

  const startEdit = (conn: Connection, fresh: boolean) => {
    setDraft({ ...conn })
    setIsNew(fresh)
    setTestMsg(null)
  }

  const saveDraft = () => {
    if (!draft) return
    if (!draft.name.trim() && !draft.baseUrl.trim()) return
    persistConnection(draft)
    if (isNew) activate(draft.id) // 新增的连接直接激活
    setDraft(null)
    setIsNew(false)
    setTestMsg(null)
  }

  const runTest = async () => {
    if (!draft) return
    setTesting(true)
    setTestMsg(null)
    const r = await testConnection(draft)
    setTestMsg({ ok: r.ok, text: r.message })
    setTesting(false)
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        aria-label="连接设置"
        title={`连接设置${active ? `（当前：${active.name}${hasKey ? '，已配置 Key' : ''}）` : ''}`}
        className={`btn-press w-8 h-8 rounded-lg flex items-center justify-center text-sm border transition shadow-sm ${
          open
            ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300 shadow-[0_0_12px_rgba(16,185,129,0.3)]'
            : hasKey
              ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-400 hover:bg-emerald-500/20'
              : 'bg-zinc-900 border-white/[0.08] text-zinc-400 hover:text-zinc-200 hover:border-emerald-500/30'
        }`}
      >
        🔑
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-2 w-[26rem] bg-zinc-950/95 backdrop-blur-2xl rounded-2xl shadow-2xl border border-white/[0.1] p-4 z-50 text-zinc-200 animate-view-enter">
          <div className="text-xs text-zinc-400 mb-2 font-mono flex items-center justify-between">
            <span>已保存连接（点击切换）</span>
            <span className="text-[11px] text-zinc-500">{conns.length} 个配置</span>
          </div>
          <ul className="space-y-1.5 max-h-52 overflow-y-auto custom-scroll">
            {conns.map((c) => {
              const on = c.id === activeId
              return (
                <li
                  key={c.id}
                  className={`flex items-center gap-1.5 rounded-xl border px-3 py-2 transition ${
                    on ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-white/[0.06] bg-zinc-900/60 hover:bg-zinc-900/90'
                  }`}
                >
                  <button
                    onClick={() => activate(c.id)}
                    className="flex-1 text-left min-w-0"
                    title="切换到此连接"
                  >
                    <span className="flex items-center gap-1.5">
                      <span className={`w-1.5 h-1.5 rounded-full ${on ? 'bg-emerald-400 animate-pulse' : 'bg-zinc-600'}`} />
                      <span className="text-sm font-medium text-zinc-200 truncate">{c.name}</span>
                      {c.apiKey && <span className="text-xs text-emerald-400" title="已配置 Key">🔑</span>}
                    </span>
                    <span className="block text-xs text-zinc-400 font-mono truncate mt-0.5">
                      {c.baseUrl || '相对路径（本机/代理）'}
                    </span>
                  </button>
                  <button
                    onClick={() => startEdit(c, false)}
                    className="btn-press px-2 py-1 text-xs text-zinc-400 hover:text-emerald-400 hover:bg-zinc-800 rounded font-mono transition"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => {
                      if (window.confirm(`删除连接「${c.name}」？`)) deleteConnection(c.id)
                    }}
                    className="btn-press px-1.5 py-1 text-xs text-zinc-500 hover:text-rose-400 hover:bg-rose-500/10 rounded transition"
                    aria-label={`删除 ${c.name}`}
                  >
                    ✕
                  </button>
                </li>
              )
            })}
          </ul>

          {!draft && (
            <button
              onClick={() => startEdit(createConnection('', '', ''), true)}
              className="mt-2.5 w-full py-1.5 text-xs rounded-xl border border-dashed border-white/[0.1] text-zinc-400 hover:border-emerald-500/40 hover:text-emerald-400 transition font-mono"
            >
              ＋ 新增连接
            </button>
          )}

          {/* 操作者名称：全局设置（非每连接），随写操作以 X-Operator 头发送，
              brain 审计流水/审批记录据此落操作者归属 */}
          <OperatorField />

          {draft && (
            <div className="border-t border-white/[0.07] mt-3 pt-3 space-y-2">
              <input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="名称（如 本机-8000）"
                className="w-full px-2.5 py-1.5 text-xs bg-zinc-900 border border-white/[0.08] text-zinc-200 placeholder-zinc-500 rounded-lg focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
              />
              <input
                value={draft.baseUrl}
                onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
                placeholder="地址（如 http://127.0.0.1:7001；留空 = 相对路径）"
                className="w-full px-2.5 py-1.5 text-xs bg-zinc-900 border border-white/[0.08] text-zinc-200 placeholder-zinc-500 rounded-lg font-mono focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
              />
              <input
                type="password"
                value={draft.apiKey}
                onChange={(e) => setDraft({ ...draft, apiKey: e.target.value })}
                placeholder="API Key（brain 的 ORCH_API_KEY；留空 = 无鉴权）"
                className="w-full px-2.5 py-1.5 text-xs bg-zinc-900 border border-white/[0.08] text-zinc-200 placeholder-zinc-500 rounded-lg font-mono focus:outline-none focus:ring-1 focus:ring-emerald-500/50"
              />
              <div className="flex items-center gap-2 pt-1">
                <button
                  onClick={saveDraft}
                  className="btn-press px-3.5 py-1.5 text-xs rounded-lg bg-emerald-500 text-zinc-950 hover:bg-emerald-400 font-semibold transition"
                >
                  {isNew ? '保存并切换' : '保存'}
                </button>
                <button
                  onClick={() => { setDraft(null); setIsNew(false); setTestMsg(null) }}
                  className="btn-press px-3 py-1.5 text-xs rounded-lg border border-white/[0.08] text-zinc-400 hover:bg-zinc-800 transition"
                >
                  取消
                </button>
                <button
                  onClick={runTest}
                  disabled={testing}
                  className="btn-press px-3 py-1.5 text-xs rounded-lg border border-sky-500/30 text-sky-400 hover:bg-sky-500/10 transition disabled:opacity-50"
                >
                  {testing ? '测试中…' : '测试连接'}
                </button>
                {testMsg && (
                  <span className={`text-xs font-mono ml-auto ${testMsg.ok ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {testMsg.text}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}