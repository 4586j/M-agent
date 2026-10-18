import { useState } from 'react'
import type { ProviderList } from '../types'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { POLL } from '../polling'

/** 运行时 Provider 管理：模型/请求地址/密钥（网关级配置）。
 * 密钥只写 brain providers.json，此页回显已打码；${ENV} 引用在派发时由环境解析。
 * 热切：设默认只影响后续新任务，running/排队任务用已解析值。 */
export default function Providers() {
  const { data, error, refresh } = usePolling<ProviderList>(() => api.providers(), POLL.services)
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
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-xs text-zinc-400">
          模型 / 请求地址 / 密钥（Provider）池——任务按 <b className="text-zinc-200">显式 provider &gt; 默认 &gt; 节点自身配置</b> 解析；
          密钥不落任务记录/审计，建议用 <code className="bg-zinc-800/80 border border-white/[0.08] px-1.5 py-0.5 rounded text-emerald-400 font-mono">$&#123;ENV&#125;</code> 引用环境变量
        </p>
        <button
          type="button"
          onClick={() => setCreating(!creating)}
          disabled={busy}
          className="btn-press px-4 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold transition disabled:opacity-40 shadow-[0_0_15px_rgba(16,185,129,0.3)]"
        >
          {creating ? '收起表单' : '+ 新增 Provider'}
        </button>
      </div>

      {msg && (
        <div className="text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 rounded-xl px-3 py-2 font-mono">
          {msg}
        </div>
      )}
      {error && (
        <div className="text-rose-400 text-xs bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">
          加载失败：{error}（请确认 brain_api 已启动）
        </div>
      )}

      {creating && (
        <ProviderForm
          onSaved={() => { setMsg('已保存 Provider'); setCreating(false); refresh() }}
        />
      )}

      <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.07] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-2">
        {Object.keys(providers).length === 0 && !error ? (
          <div className="text-sm text-zinc-500 text-center py-12">
            暂无 Provider——「新增 Provider」登记一个网络网关（名称/模型/端点/密钥）
          </div>
        ) : (
          <div className="divide-y divide-white/[0.04]">
            {Object.entries(providers).map(([name, p]) => (
              <div
                key={name}
                className="px-4 py-3.5 flex flex-col md:flex-row md:items-center gap-3 hover:bg-zinc-800/40 rounded-xl transition duration-150"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    {currentDefault === name && (
                      <span
                        className="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 font-mono"
                        title="当前默认：后续新任务未指名时用此 Provider"
                      >
                        默认
                      </span>
                    )}
                    <span className="font-semibold text-zinc-100 text-sm font-mono">{name}</span>
                    <span className="font-mono text-xs bg-zinc-800/80 text-zinc-300 border border-white/[0.06] px-2 py-0.5 rounded-md">
                      {p.model || '默认模型'}
                    </span>
                  </div>
                  <div className="text-xs text-zinc-400 mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono">
                    <span>端点：<span className="text-zinc-300">{p.base_url || '进程 env 默认'}</span></span>
                    <span>
                      密钥：
                      <span className="text-zinc-400" title={p.auth_token ? '已配置（掩码显示，明文只存 brain providers.json）' : ''}>
                        {p.auth_token || '未配置'}
                      </span>
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  {currentDefault !== name && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => act(`设为默认（${name}）`, () => api.setDefaultProvider(name))}
                      className="btn-press px-3 py-1 rounded-lg border border-white/[0.08] bg-zinc-950/70 text-zinc-300 hover:bg-zinc-800 hover:text-emerald-400 text-xs font-medium disabled:opacity-40 transition"
                    >
                      设为默认
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      if (!window.confirm(`删除 Provider [${name}] 吗？\n引用它的后续派发将改用默认/节点自身配置。`)) return
                      act('删除', () => api.deleteProvider(name))
                    }}
                    className="btn-press px-3 py-1 rounded-lg border border-rose-500/20 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 text-xs font-medium disabled:opacity-40 transition"
                  >
                    删除
                  </button>
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
    <div className="spotlight-card bg-zinc-900/40 backdrop-blur-xl rounded-2xl border border-white/[0.08] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)] p-5 space-y-3.5">
      <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
        新增 Provider
      </div>
      <div className="grid grid-cols-1 md:grid-cols-4 gap-2.5">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="名称（如 gw-main）"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="模型（如 sonnet）"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="端点（如 https://gw.example.com/v1）"
          className="px-3 py-2 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
        />
        <div className="relative">
          <input
            value={authToken}
            type={showToken ? 'text' : 'password'}
            onChange={(e) => setAuthToken(e.target.value)}
            placeholder="密钥（${ENV} 或明文）"
            className="w-full px-3 py-2 pr-16 text-xs border border-white/[0.08] rounded-xl font-mono bg-zinc-950 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-emerald-500/40"
          />
          <button
            type="button"
            onClick={() => setShowToken(!showToken)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-zinc-400 hover:text-zinc-200 font-mono"
          >
            {showToken ? '隐藏' : '显示'}
          </button>
        </div>
      </div>
      <div className="text-xs text-zinc-500 font-mono">
        模型留空 = 节点人设/上层默认；端点留空 = 进程 env 的 ANTHROPIC_BASE_URL；
        密钥支持 $&#123;ENV_VAR&#125; 引用环境变量（推荐，避免明文）。
      </div>
      {err && <div className="text-xs text-rose-400 font-mono">{err}</div>}
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          disabled={busy || !name.trim()}
          onClick={submit}
          className="px-5 py-1.5 text-xs rounded-xl bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-semibold disabled:opacity-40 transition shadow-[0_0_15px_rgba(16,185,129,0.3)]"
        >
          保存 Provider
        </button>
      </div>
    </div>
  )
}