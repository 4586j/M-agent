import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Routes, Route, NavLink, useLocation, Link } from 'react-router-dom'
import { usePolling } from './hooks/usePolling'
import { api } from './api/client'
import { useActiveConnection } from './api/connections'
import Dashboard from './views/Dashboard'
import Tasks from './views/Tasks'
import NodeManage from './views/NodeManage'
import Command from './views/Command'
import Memory from './views/Memory'
import Schedules from './views/Schedules'
import Audit from './views/Audit'
import Providers from './views/Providers'
import ConnectionSettings from './components/ConnectionSettings'

const NAV = [
  { to: '/', label: '总览', end: true },
  { to: '/tasks', label: '任务' },
  { to: '/nodes', label: '节点' },
  { to: '/command', label: '命令台' },
  { to: '/memory', label: '记忆' },
  { to: '/schedules', label: '调度' },
  { to: '/providers', label: 'Provider' },
  { to: '/audit', label: '审计' },
]

/** 生机绿白顶栏：玻璃拟态 + 7 视图滑动 glass pill + SSE 呼吸状态点 */
export default function App() {
  return (
    <div className="min-h-screen bg-gradient-to-b from-mint-50 to-[#F8FCFA] text-slate-800">
      <TopBar />
      <main className="p-6 max-w-7xl mx-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/nodes" element={<NodeManage />} />
          <Route path="/command" element={<Command />} />
          <Route path="/memory" element={<Memory />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/providers" element={<Providers />} />
          <Route path="/audit" element={<Audit />} />
        </Routes>
      </main>
    </div>
  )
}

function TopBar() {
  const location = useLocation()
  const navRef = useRef<HTMLDivElement>(null)
  const [pill, setPill] = useState({ left: 0, width: 0 })
  const stats = usePolling(() => api.stats(), 10000)

  // 滑动玻璃指示块：随激活项移动（含 resize 重校准）
  useLayoutEffect(() => {
    const el = navRef.current?.querySelector<HTMLElement>('a[data-active="true"]')
    if (el) setPill({ left: el.offsetLeft, width: el.offsetWidth })
  }, [location])
  useEffect(() => {
    const onResize = () => {
      const el = navRef.current?.querySelector<HTMLElement>('a[data-active="true"]')
      if (el) setPill({ left: el.offsetLeft, width: el.offsetWidth })
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const connected = !stats.error
  const activeConn = useActiveConnection()

  return (
    <header className="h-14 bg-white/85 backdrop-blur-md border-b border-mint-100 sticky top-0 z-40 px-6 flex items-center justify-between shadow-mint-sm">
      <div className="flex items-center gap-8">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-mint-500 to-mint-400 flex items-center justify-center text-white shadow-mint-glow">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <span className="font-bold text-base text-slate-800 tracking-tight hidden sm:inline">AI 编排控制面板</span>
        </Link>

        <nav ref={navRef} className="relative p-1 bg-mint-50/70 border border-mint-200/50 rounded-xl hidden md:flex items-center">
          <div
            className="absolute top-1 bottom-1 rounded-lg bg-white/85 backdrop-blur border border-mint-200/60 shadow-mint-sm pointer-events-none z-0 transition-all duration-300"
            style={{ left: pill.left, width: pill.width }}
          />
          {NAV.map((item) => {
            const active = item.end
              ? location.pathname === '/'
              : location.pathname.startsWith(item.to)
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                data-active={active}
                className={`relative z-10 px-3.5 py-1 text-sm transition-colors duration-200 rounded-lg ${
                  active ? 'font-semibold text-mint-700' : 'font-medium text-slate-600 hover:text-mint-700'
                }`}
              >
                {item.label}
              </NavLink>
            )
          })}
        </nav>
      </div>

      <div className="flex items-center gap-3">
        {/* 当前连接的 brain 名（多 brain 切换后跟随） */}
        <div
          className="hidden md:flex items-center gap-1.5 px-3 py-1 rounded-full text-xs border border-slate-200/70 bg-white/70 text-slate-600 max-w-[10rem]"
          title="当前连接的 brain；点 🔑 管理/切换"
        >
          <span className="truncate">{activeConn.name || '默认'}</span>
          {activeConn.baseUrl && (
            <span className="text-[10px] text-slate-400 font-mono truncate">{activeConn.baseUrl}</span>
          )}
        </div>
        {/* SSE / 大脑连接状态：呼吸脉冲点 */}
        <div
          className={`hidden sm:flex items-center gap-2 px-3 py-1 rounded-full text-xs border ${
            connected
              ? 'bg-mint-50/80 border-mint-200/60 text-mint-700'
              : 'bg-rose-50 border-rose-200 text-rose-600'
          }`}
          title={connected ? '大脑连接正常' : '大脑连接失败（任务/统计为空或过期）'}
        >
          <span className={`w-2 h-2 rounded-full ${connected ? 'bg-mint-500 animate-pulse' : 'bg-rose-500'}`} />
          <span className="font-medium">{connected ? '已连接' : '连接失败'}</span>
        </div>
        <Link
          to="/command"
          className="px-3.5 py-1.5 bg-mint-600 hover:bg-mint-500 text-white rounded-lg text-xs font-semibold shadow-sm transition flex items-center gap-1.5"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          新派发
        </Link>
        <ConnectionSettings />
      </div>
    </header>
  )
}
