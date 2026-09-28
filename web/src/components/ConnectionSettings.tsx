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
        className={`w-8 h-8 rounded-lg flex items-center justify-center text-sm border transition ${
          open
            ? 'bg-mint-100 border-mint-300 text-mint-700'
            : hasKey
              ? 'bg-mint-50 border-mint-200 text-mint-600 hover:bg-mint-100'
              : 'bg-white border-slate-200 text-slate-400 hover:text-slate-600 hover:border-mint-300'
        }`}
      >
        🔑
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-2 w-[26rem] bg-white rounded-xl shadow-mint-card border border-mint-100 p-3 z-50">
          <div className="text-xs text-slate-500 mb-1">已保存连接（点击切换）</div>
          <ul className="space-y-1 max-h-52 overflow-y-auto">
            {conns.map((c) => {
              const on = c.id === activeId
              return (
                <li
                  key={c.id}
                  className={`flex items-center gap-1 rounded-lg border px-2 py-1.5 transition ${
                    on ? 'border-mint-300 bg-mint-50' : 'border-slate-200 bg-white'
                  }`}
                >
                  <button
                    onClick={() => activate(c.id)}
                    className="flex-1 text-left min-w-0"
                    title="切换到此连接"
                  >
                    <span className="flex items-center gap-1.5">
                      <span className={`w-1.5 h-1.5 rounded-full ${on ? 'bg-mint-500' : 'bg-slate-300'}`} />
                      <span className="text-sm font-medium text-slate-700 truncate">{c.name}</span>
                      {c.apiKey && <span className="text-xs text-mint-500" title="已配置 Key">🔑</span>}
                    </span>
                    <span className="block text-xs text-slate-400 font-mono truncate">
                      {c.baseUrl || '相对路径（本机/代理）'}
                    </span>
                  </button>
                  <button
                    onClick={() => startEdit(c, false)}
                    className="px-2 py-1 text-xs text-slate-500 hover:text-mint-700 hover:bg-mint-50 rounded"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => {
                      if (window.confirm(`删除连接「${c.name}」？`)) deleteConnection(c.id)
                    }}
                    className="px-1.5 py-1 text-xs text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded"
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
              className="mt-2 w-full py-1.5 text-xs rounded-lg border border-dashed border-slate-300 text-slate-500 hover:border-mint-300 hover:text-mint-700 transition"
            >
              ＋ 新增连接
            </button>
          )}

          {draft && (
            <div className="border-t border-slate-100 mt-2 pt-2 space-y-1.5">
              <input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="名称（如 本机-8000）"
                className="w-full px-2 py-1 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-mint-400"
              />
              <input
                value={draft.baseUrl}
                onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
                placeholder="地址（如 http://127.0.0.1:7001；留空 = 相对路径）"
                className="w-full px-2 py-1 text-sm border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400"
              />
              <input
                type="password"
                value={draft.apiKey}
                onChange={(e) => setDraft({ ...draft, apiKey: e.target.value })}
                placeholder="API Key（brain 的 ORCH_API_KEY；留空 = 无鉴权）"
                className="w-full px-2 py-1 text-sm border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={saveDraft}
                  className="px-3 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold transition"
                >
                  {isNew ? '保存并切换' : '保存'}
                </button>
                <button
                  onClick={() => { setDraft(null); setIsNew(false); setTestMsg(null) }}
                  className="px-3 py-1.5 text-xs rounded-lg border border-slate-200 text-slate-500 hover:bg-slate-50 transition"
                >
                  取消
                </button>
                <button
                  onClick={runTest}
                  disabled={testing}
                  className="px-3 py-1.5 text-xs rounded-lg border border-sky-200 text-sky-600 hover:bg-sky-50 transition disabled:opacity-50"
                >
                  {testing ? '测试中…' : '测试连接'}
                </button>
                {testMsg && (
                  <span className={`text-xs ${testMsg.ok ? 'text-mint-700' : 'text-rose-600'}`}>
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