import { useCallback, useEffect, useRef, useState } from 'react'
import { getActiveConnectionScope, useActiveConnectionScope } from '../api/connections'

/**
 * 周期轮询 + 手动刷新。fn 引用变化不影响定时器（用 ref 持有最新值）。
 *
 * 多 brain：切换连接或修改地址/密钥时自动清空旧数据并立即重拉，
 * 全部视图跟随切换。epoch 守卫防竞态——旧连接的请求若在切换后迟到
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
  const requestRef = useRef(0)
  const settledRef = useRef(0)
  const scope = useActiveConnectionScope()
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const dataScopeRef = useRef(scope)

  const tick = useCallback(async () => {
    if (!enabledRef.current) return
    const expectedScope = scopeRef.current
    if (getActiveConnectionScope() !== expectedScope) return
    const epoch = epochRef.current
    const request = ++requestRef.current
    const valid = () => epoch === epochRef.current && request >= settledRef.current
      && getActiveConnectionScope() === expectedScope
    try {
      const d = await fnRef.current()
      if (!valid()) return
      settledRef.current = request
      setData(d)
      setError(null)
    } catch (e) {
      if (!valid()) return
      settledRef.current = request
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (valid()) setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 切换连接：清空旧连接数据（避免新数据落地前短暂显示上一个 brain 的内容）
    epochRef.current += 1
    dataScopeRef.current = scope
    setData(null)
    setError(null)
    setLoading(true)
  }, [scope])

  useEffect(() => {
    if (!enabled) return
    let alive = true
    const localTick = async () => {
      if (alive) await tick()
    }
    localTick()
    const h = setInterval(localTick, intervalMs)
    return () => {
      alive = false
      epochRef.current += 1
      clearInterval(h)
    }
  }, [intervalMs, tick, scope, enabled])

  const current = dataScopeRef.current === scope
  return { data: current ? data : null, error: current ? error : null,
    loading: current ? loading : true, refresh: tick, setData }
}
