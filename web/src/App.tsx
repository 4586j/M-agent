import { useEffect, useState } from 'react'
import { Routes, Route, NavLink, useLocation, Link } from 'react-router-dom'
import { StatsProvider, useSharedStats } from './hooks/useSharedStats'
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
import CommandPalette from './components/CommandPalette'

interface NavGroup {
  domain: string
  items: {
    to: string
    label: string
    end?: boolean
    icon: React.ReactNode
  }[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    domain: '指挥监控',
    items: [
      {
        to: '/',
        label: '总览',
        end: true,
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
          </svg>
        ),
      },
      {
        to: '/command',
        label: '命令台',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        ),
      },
      {
        to: '/tasks',
        label: '任务',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
          </svg>
        ),
      },
    ],
  },
  {
    domain: '算力与调度',
    items: [
      {
        to: '/nodes',
        label: '节点',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
          </svg>
        ),
      },
      {
        to: '/schedules',
        label: '调度',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        ),
      },
      {
        to: '/providers',
        label: 'Provider',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
        ),
      },
    ],
  },
  {
    domain: '记忆与审计',
    items: [
      {
        to: '/memory',
        label: '记忆',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        ),
      },
      {
        to: '/audit',
        label: '审计',
        icon: (
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        ),
      },
    ],
  },
]

export default function App() {
  return (
    <StatsProvider>
      <AppShell />
    </StatsProvider>
  )
}

function AppShell() {
  const location = useLocation()
  const [cmdOpen, setCmdOpen] = useState(false)
  const stats = useSharedStats()
  const activeConn = useActiveConnection()
  const connected = !stats.error

  // ⌘K / Ctrl+K 全局快捷键
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCmdOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#09090b] text-zinc-100 antialiased selection:bg-emerald-500/20 selection:text-emerald-400">
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} />

      {/* 左侧固定导航栏（w-64） */}
      <aside className="w-64 bg-zinc-950/80 backdrop-blur-xl border-r border-white/[0.07] flex flex-col shrink-0 h-screen select-none z-20">
        {/* 顶部 Logo 与集群状态 */}
        <Link to="/" className="p-4 flex items-center gap-3 border-b border-white/[0.07] group">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-emerald-600 via-emerald-500 to-teal-400 flex items-center justify-center text-zinc-950 shadow-[0_0_20px_rgba(16,185,129,0.3)] group-hover:scale-105 transition">
            <svg className="w-5 h-5 font-bold" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
          <div className="min-w-0">
            <div className="font-bold text-sm text-zinc-100 tracking-tight flex items-center gap-1.5">
              <span>M-Agent</span>
              <span className="text-[10px] font-mono font-medium px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">v2.0</span>
            </div>
            <div className="text-[11px] text-zinc-500 font-mono flex items-center gap-1.5 mt-0.5">
              <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400 animate-pulse shadow-[0_0_6px_rgba(52,211,153,0.8)]' : 'bg-rose-500'}`} />
              <span>{connected ? 'Cluster Active' : 'Offline'}</span>
            </div>
          </div>
        </Link>

        {/* 业务域分组导航项 */}
        <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-6 custom-scroll">
          {NAV_GROUPS.map((grp) => (
            <div key={grp.domain} className="space-y-1">
              <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider px-3 mb-1.5 font-mono">
                {grp.domain}
              </div>
              {grp.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs font-medium transition duration-150 border ${
                      isActive
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20 font-semibold shadow-[0_0_15px_rgba(16,185,129,0.08)]'
                        : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60 border-transparent'
                    }`
                  }
                >
                  {item.icon}
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {/* 底部内嵌浓缩版 ConnectionSettings 状态栏 */}
        <div className="p-3.5 border-t border-white/[0.07] bg-zinc-950/60 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <span className={`w-2 h-2 rounded-full shrink-0 ${connected ? 'bg-emerald-400' : 'bg-rose-500'}`} />
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-zinc-200 truncate">{activeConn.name || '默认'}</div>
              <div className="text-[10px] text-zinc-500 font-mono truncate">{activeConn.baseUrl || '相对路径'}</div>
            </div>
          </div>
          <ConnectionSettings />
        </div>
      </aside>

      {/* 右侧全屏自适应工作区 */}
      <div className="flex-1 flex flex-col h-screen overflow-hidden min-w-0">
        {/* 顶部 h-16 毛玻璃 Header（满足 E2E 包含「编排控制面板」选择器） */}
        <header className="h-16 bg-zinc-950/80 backdrop-blur-xl border-b border-white/[0.07] px-6 flex items-center justify-between shrink-0 z-30">
          <div className="flex items-center gap-3 min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-zinc-100 tracking-tight">
              <span className="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]" />
              <span>编排控制面板</span>
            </div>
            <span className="text-zinc-700">/</span>
            <span className="text-xs text-zinc-400 font-mono truncate">
              {getViewLabel(location.pathname)}
            </span>
          </div>

          <div className="flex items-center gap-3">
            {/* ⌘K 搜索胶囊 */}
            <button
              type="button"
              onClick={() => setCmdOpen(true)}
              className="hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-xl text-xs bg-zinc-900/80 hover:bg-zinc-800 border border-white/[0.08] text-zinc-400 hover:text-zinc-200 transition font-mono"
              title="快捷键 ⌘K / Ctrl+K 搜索"
            >
              <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <span>搜索指令...</span>
              <kbd className="px-1.5 py-0.5 text-[10px] bg-zinc-800 rounded border border-white/[0.1] text-zinc-400 font-mono">⌘K</kbd>
            </button>

            {/* 新派发快捷按钮 */}
            <Link
              to="/command"
              className="btn-press px-3.5 py-1.5 bg-emerald-500 hover:bg-emerald-400 text-zinc-950 rounded-xl text-xs font-semibold shadow-[0_0_15px_rgba(16,185,129,0.25)] transition flex items-center gap-1.5"
            >
              <svg className="w-3.5 h-3.5 font-bold" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.4} d="M12 4v16m8-8H4" />
              </svg>
              新派发
            </Link>
          </div>
        </header>

        {/* 主视口区域 */}
        <main className="flex-1 overflow-y-auto p-6 custom-scroll bg-dot-grid">
          <div className="max-w-7xl mx-auto">
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
          </div>
        </main>
      </div>
    </div>
  )
}

function getViewLabel(pathname: string): string {
  if (pathname === '/') return '总览 Dashboard'
  if (pathname.startsWith('/tasks')) return '任务管理 Tasks'
  if (pathname.startsWith('/nodes')) return '节点集群 Nodes'
  if (pathname.startsWith('/command')) return '命令调度台 Command'
  if (pathname.startsWith('/memory')) return '记忆中心 Memory'
  if (pathname.startsWith('/schedules')) return '定时调度 Schedules'
  if (pathname.startsWith('/providers')) return '模型 Provider'
  if (pathname.startsWith('/audit')) return '安全审计流水 Audit'
  return '控制台'
}
