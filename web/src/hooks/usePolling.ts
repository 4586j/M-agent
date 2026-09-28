import { useCallback, useEffect, useRef, useState } from 'react'
import { useActiveConnectionId } from '../api/connections'

/**
 * 周期轮询 + 手动刷新。fn 引用变化不影响定时器（用 ref 持有最新值）。
 *
 * 多 brain：切换活动连接时自动清空旧数据并立即重拉（connId 入定时器 deps，
 * 全部视图零改动跟随切换）。epoch 守卫防竞态——旧连接的请求若在切换后迟到
 * 落地，会被 epoch 比对丢弃，绝不让旧 brain 数据覆盖新连接的数据。
 */
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fnRef = useRef(fn)
  fnRef.current = fn
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  // 轮询 epoch：切换连接/重置时递增。tick 捕获调用时点，响应落地时与当前
  // 比对，不一致即丢弃（旧连接的 in-flight 请求不能污染新连接）。
  const epochRef = useRef(0)

  const tick = useCallback(async () => {
    if (!enabledRef.current) return
    const epoch = epochRef.current
    try {
      const d = await fnRef.current()
      if (epoch !== epochRef.current) return
      setData(d)
      setError(null)
    } catch (e) {
      if (epoch !== epochRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (epoch === epochRef.current) setLoading(false)
    }
  }, [])

  const connId = useActiveConnectionId()

  useEffect(() => {
    // 切换连接：清空旧连接数据（避免新数据落地前短暂显示上一个 brain 的内容）
    epochRef.current += 1
    setData(null)
    setError(null)
    setLoading(true)
  }, [connId])

  useEffect(() => {
    let alive = true
    const localTick = async () => {
      if (alive) await tick()
    }
    localTick()
    const h = setInterval(localTick, intervalMs)
    return () => {
      alive = false
      clearInterval(h)
    }
  }, [intervalMs, tick, connId])

  return { data, error, loading, refresh: tick, setData }
}
