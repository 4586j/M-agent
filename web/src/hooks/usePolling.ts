import { useCallback, useEffect, useRef, useState } from 'react'

/** 周期轮询 + 手动刷新。fn 引用变化不影响定时器（用 ref 持有最新值）。 */
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fnRef = useRef(fn)
  fnRef.current = fn
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const tick = useCallback(async () => {
    if (!enabledRef.current) return
    try {
      const d = await fnRef.current()
      setData(d)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

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
  }, [intervalMs, tick])

  return { data, error, loading, refresh: tick, setData }
}
