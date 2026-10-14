import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

interface CommandItem {
  id: string
  label: string
  sublabel?: string
  icon?: string
  to?: string
  action?: () => void
}

const ITEMS: CommandItem[] = [
  { id: 'dash', label: '总览 Dashboard', sublabel: '全局指标与待办中心', icon: '📊', to: '/' },
  { id: 'tasks', label: '任务 Tasks', sublabel: '任务全集与实时 SSE 抽屉', icon: '📋', to: '/tasks' },
  { id: 'command', label: '命令台 Command', sublabel: '派发 Prompt 与 Agent 顾问', icon: '🚀', to: '/command' },
  { id: 'nodes', label: '节点管理 Nodes', sublabel: 'Worker 注册表与 Agent 面板', icon: '🖥️', to: '/nodes' },
  { id: 'memory', label: '记忆中心 Memory', sublabel: '核心记忆、经验库与演化提案', icon: '🧠', to: '/memory' },
  { id: 'schedules', label: '调度管理 Schedules', sublabel: 'Cron 定时任务管理', icon: '⏱️', to: '/schedules' },
  { id: 'providers', label: '模型 Provider', sublabel: '模型与网关密钥配置', icon: '🌐', to: '/providers' },
  { id: 'audit', label: '安全审计 Audit', sublabel: '关卡①②与人工审批裁决流水', icon: '🛡️', to: '/audit' },
]

export default function CommandPalette({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const filtered = ITEMS.filter((item) =>
    item.label.toLowerCase().includes(q.toLowerCase()) ||
    (item.sublabel && item.sublabel.toLowerCase().includes(q.toLowerCase()))
  )

  useEffect(() => {
    if (open) {
      setQ('')
      setSelectedIndex(0)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  useEffect(() => {
    setSelectedIndex(0)
  }, [q])

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!open) return
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIndex((i) => (filtered.length ? (i + 1) % filtered.length : 0))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIndex((i) => (filtered.length ? (i - 1 + filtered.length) % filtered.length : 0))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        const selected = filtered[selectedIndex]
        if (selected) {
          if (selected.to) navigate(selected.to)
          if (selected.action) selected.action()
          onClose()
        }
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [open, filtered, selectedIndex, navigate, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-20 px-4">
      {/* 半透明磨砂背景遮罩 */}
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-md transition-opacity"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* 命令面板主体 */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="全局命令调度盘"
        className="relative w-full max-w-xl bg-zinc-950/95 backdrop-blur-2xl rounded-2xl shadow-2xl border border-white/[0.1] overflow-hidden z-10 animate-in fade-in zoom-in-95 duration-200"
      >
        <div className="flex items-center px-4 py-3.5 border-b border-white/[0.07] bg-zinc-900/40">
          <svg className="w-5 h-5 text-emerald-400 mr-3 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索视图或输入指令 (如: 总览, 任务, 节点...)"
            className="w-full bg-transparent border-none text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none"
          />
          <kbd className="px-1.5 py-0.5 text-[10px] bg-zinc-800 text-zinc-400 rounded border border-white/[0.08] font-mono">
            ESC
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-2 custom-scroll">
          {filtered.length === 0 ? (
            <div className="p-4 text-center text-xs text-zinc-500 font-mono">未找到匹配指令</div>
          ) : (
            filtered.map((item, idx) => {
              const active = idx === selectedIndex
              return (
                <div
                  key={item.id}
                  onClick={() => {
                    if (item.to) navigate(item.to)
                    if (item.action) item.action()
                    onClose()
                  }}
                  onMouseEnter={() => setSelectedIndex(idx)}
                  className={`flex items-center justify-between px-3.5 py-2.5 rounded-xl cursor-pointer transition text-xs border ${
                    active
                      ? 'bg-emerald-500/10 text-emerald-400 font-semibold border-emerald-500/20 shadow-[0_0_15px_rgba(16,185,129,0.1)]'
                      : 'text-zinc-300 hover:bg-zinc-900/60 border-transparent'
                  }`}
                >
                  <div className="flex items-center gap-2.5">
                    <span className="text-base">{item.icon}</span>
                    <div>
                      <div className="font-medium text-zinc-100">{item.label}</div>
                      {item.sublabel && (
                        <div className="text-[11px] text-zinc-500">{item.sublabel}</div>
                      )}
                    </div>
                  </div>
                  {active && (
                    <span className="text-[10px] text-emerald-400 bg-emerald-500/15 px-2 py-0.5 rounded-md border border-emerald-500/30 font-mono font-medium">
                      ↵ 打开
                    </span>
                  )}
                </div>
              )
            })
          )}
        </div>

        <div className="px-4 py-2.5 border-t border-white/[0.07] bg-zinc-900/40 flex items-center justify-between text-[11px] text-zinc-500 font-mono">
          <span>使用 ↑ ↓ 键移动，Enter 选择</span>
          <span>按 ⌘K 随时唤起</span>
        </div>
      </div>
    </div>
  )
}
