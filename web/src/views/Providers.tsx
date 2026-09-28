import { useState } from 'react'
import type { ProviderList } from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'

/** 运行时 Provider 管理：模型/请求地址/密钥（网关级配置）。
 * 密钥只写 brain providers.json，此页回显已打码；${ENV} 引用在派发时由环境解析。
 * 热切：设默认只影响后续新任务，running/排队任务用已解析值。 */
export default function Providers() {
  const { data, error, refresh } = usePolling<ProviderList>(() => api.providers(), 8000)
  const [creating, setCreating] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const providers = data?.providers ?? {}
  const currentDefault = data?.default ?? ''

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true)
    setMsg(null)
    try {
      const r = await fn()
      setMsg(`${label} 完成`)
      void r
    } catch (e) {
      setMsg(`${label} 失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
      refresh()
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">
          模型 / 请求地址 / 密钥（Provider）池——任务按 <b>显式 provider &gt; 默认 &gt; 节点自身配置</b> 解析；
          密钥不落任务记录/审计，建议用 <code className="bg-slate-100 px-1 rounded">$&#123;ENV&#125;</code> 引用环境变量
        </p>
        <button onClick={() => setCreating(!creating)} disabled={busy}
          className="px-3.5 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold transition disabled:opacity-40">
          {creating ? '收起表单' : '+ 新增 Provider'}
        </button>
      </div>

      {msg && <div className="text-xs text-mint-700 bg-mint-50 border border-mint-200 rounded-xl px-3 py-2">{msg}</div>}
      {error && (
        <div className="text-rose-600 text-sm bg-rose-50 border border-rose-200 rounded-xl p-2.5">
          加载失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {creating && (
        <ProviderForm
          onSaved={() => { setMsg('已保存 Provider'); setCreating(false); refresh() }}
        />
      )}

      <div className="bg-white rounded-2xl border border-mint-100 shadow-mint-card p-2">
        {Object.keys(providers).length === 0 && !error ? (
          <div className="text-sm text-slate-400 text-center py-10">
            暂无 Provider——「新增 Provider」登记一个网络网关（名称/模型/端点/密钥）
          </div>
        ) : (
          <div className="divide-y divide-slate-50">
            {Object.entries(providers).map(([name, p]) => (
              <div key={name} className="px-3 py-3 flex flex-col md:flex-row md:items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    {currentDefault === name && (
                      <span className="px-2 py-0.5 rounded text-xs font-bold bg-amber-100 text-amber-700"
                        title="当前默认：后续新任务未指名时用此 Provider">默认</span>
                    )}
                    <span className="font-semibold text-slate-800 text-sm">{name}</span>
                    <span className="font-mono text-xs bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">
                      {p.model || '默认模型'}
                    </span>
                  </div>
                  <div className="text-xs text-slate-500 mt-1 flex flex-wrap gap-x-3">
                    <span>端点：<span className="font-mono">{p.base_url || '进程 env 默认'}</span></span>
                    <span>密钥：<span className="font-mono" title={p.auth_token ? '已配置（掩码显示，明文只存 brain providers.json）' : ''}>{p.auth_token || '未配置'}</span></span>
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {currentDefault !== name && (
                    <button disabled={busy}
                      onClick={() => act(`设为默认（${name}）`, () => api.setDefaultProvider(name))}
                      className="px-2.5 py-1 rounded-lg border border-mint-200 text-mint-700 hover:bg-mint-50 disabled:opacity-40">
                      设为默认
                    </button>
                  )}
                  <button disabled={busy}
                    onClick={() => {
                      if (!window.confirm(`删除 Provider [${name}] 吗？\n引用它的后续派发将改用默认/节点自身配置。`)) return
                      act('删除', () => api.deleteProvider(name))
                    }}
                    className="px-2.5 py-1 rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-40">删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ProviderForm({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState('')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [authToken, setAuthToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    if (!name.trim()) { setErr('Provider 名称不能为空'); return }
    setBusy(true)
    setErr(null)
    try {
      await api.putProvider({
        name: name.trim(),
        model: model.trim() || null,
        base_url: baseUrl.trim() || null,
        auth_token: authToken.trim() || null,
      })
      onSaved()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-mint-200 shadow-mint-card p-5 space-y-3">
      <div className="text-xs font-bold text-slate-400 uppercase tracking-wider">新增 Provider</div>
      <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称（如 gw-main）"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="模型（如 sonnet）"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="端点（如 https://gw.example.com/v1）"
          className="px-2.5 py-1.5 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
        <div className="relative">
          <input value={authToken} type={showToken ? 'text' : 'password'}
            onChange={(e) => setAuthToken(e.target.value)}
            placeholder="密钥（${ENV} 或明文）"
            className="w-full px-2.5 py-1.5 pr-16 text-sm border border-mint-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400" />
          <button type="button" onClick={() => setShowToken(!showToken)}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 text-xs text-slate-400 hover:text-slate-600">
            {showToken ? '隐藏' : '显示'}
          </button>
        </div>
      </div>
      <div className="text-xs text-slate-400">
        模型留空 = 节点人设/上层默认；端点留空 = 进程 env 的 {`ANTHROPIC_BASE_URL`}；
        密钥支持 {`$&#123;ENV_VAR&#125;`} 引用环境变量（推荐，避免明文）。
      </div>
      {err && <div className="text-xs text-rose-600">{err}</div>}
      <div className="flex justify-end gap-2">
        <button disabled={busy || !name.trim()} onClick={submit}
          className="px-5 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold disabled:opacity-40">
          保存 Provider
        </button>
      </div>
    </div>
  )
}