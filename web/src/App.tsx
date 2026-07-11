import { Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './views/Dashboard'
import NodeManage from './views/NodeManage'
import Command from './views/Command'

const navCls = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'text-sky-300' : 'text-slate-300 hover:text-white'

export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="bg-slate-900 text-white px-6 py-3 flex items-center gap-8">
        <h1 className="text-base font-semibold">多 Agent 编排控制面板</h1>
        <nav className="flex gap-5 text-sm">
          <NavLink to="/" end className={navCls}>状态总览</NavLink>
          <NavLink to="/manage" className={navCls}>节点管理</NavLink>
          <NavLink to="/command" className={navCls}>命令台</NavLink>
        </nav>
      </header>
      <main className="p-6 max-w-7xl mx-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/manage" element={<NodeManage />} />
          <Route path="/command" element={<Command />} />
        </Routes>
      </main>
    </div>
  )
}
