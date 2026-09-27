import { useState } from 'react'
import { getApiKey } from '../api/client'

/**
 * 连接设置（顶栏下拉）：配置 brain 鉴权用的 API Key，存 localStorage.orch_api_key。
 * 普通请求经 api/client.ts 统一带 X-API-Key 头；SSE 走 ?api_key= 查询参数。
 * 留空保存 = 本机无 key 部署（不发鉴权头，行为与旧版一致）。
 */
export default function ConnectionSettings() {
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState(() => getApiKey())
  const [saved, setSaved] = useState(false)
  const hasKey = getApiKey() !== ''

  const save = () => {
    const trimmed = key.trim()
    localStorage.setItem('orch_api_key', trimmed)
    setKey(trimmed)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        aria-label="连接设置"
        title={`连接设置${hasKey ? '（已配置 API Key）' : ''}`}
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
        <div className="absolute right-0 top-full mt-2 w-80 bg-white rounded-xl shadow-mint-card border border-mint-100 p-3 z-50">
          <label className="text-xs text-slate-500 block">
            API Key（brain 侧配置的 ORCH_API_KEY；跨主机/带鉴权部署必填，留空 = 无鉴权部署）
            <input
              type="password"
              value={key}
              onChange={(e) => { setKey(e.target.value); setSaved(false) }}
              placeholder="留空表示本机无 key 部署"
              className="mt-1 w-full px-2 py-1 text-sm border border-slate-300 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-mint-400"
            />
          </label>
          <div className="flex items-center justify-end gap-2 mt-2">
            {saved && <span className="text-xs text-mint-700">已保存，立即生效</span>}
            <button
              onClick={save}
              className="px-3 py-1.5 text-xs rounded-lg bg-mint-600 text-white hover:bg-mint-500 font-semibold transition"
            >
              保存
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
