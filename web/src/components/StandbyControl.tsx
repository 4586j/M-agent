import {useEffect, useRef} from 'react'
import {mountStandby} from '../../../agent_core/panel/standby.js'

export default function StandbyControl() {
  const target = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (!target.current) return
    const standby = mountStandby({target: target.current, scope: 'orchestration'})
    return () => standby.destroy()
  }, [])
  return <span ref={target} className="inline-flex shrink-0" />
}
