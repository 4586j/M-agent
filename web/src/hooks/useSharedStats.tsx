import { createContext, useContext } from 'react'
import { useLocation } from 'react-router-dom'
import { usePolling } from './usePolling'
import { POLL } from '../polling'
import { api } from '../api/client'
import type { Stats } from '../types'

/** 全局唯一的 /stats 轮询源（App 顶层 Provider 注入）。
 *
 * 此前 TopBar（10s 常驻）与 Dashboard（5s）各开一路轮询，同一端点重复拉。
 * 收敛为单循环：总览页 5s（Dashboard 需要的新鲜度），离开总览页自动降频 10s
 * （TopBar 状态点够用），避免双源并发拉同接口。
 * 多 brain 切换语义沿用 usePolling（connId 变化清空旧数据 + epoch 防竞态）。
 */
export interface SharedStats {
  data: Stats | null
  error: string | null
  loading: boolean
  refresh: () => Promise<void>
}

const StatsContext = createContext<SharedStats>({
  data: null, error: null, loading: true,
  refresh: async () => {},
})

export function StatsProvider({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const onDashboard = location.pathname === '/'
  const stats = usePolling(() => api.stats(), onDashboard ? POLL.stats : POLL.statsSlow)
  return <StatsContext.Provider value={stats}>{children}</StatsContext.Provider>
}

export const useSharedStats = (): SharedStats => useContext(StatsContext)