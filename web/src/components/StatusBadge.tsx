import { statusBadge, statusDot } from '../types'

/** 状态小徽章；带状态圆点（Dot）与活体呼吸环（Pulse Ring）的半透明胶囊徽章 */
export default function StatusBadge({ status }: { status: string }) {
  const isPulse = status === 'running' || status === 'online'
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border border-white/[0.08] shrink-0 font-mono transition-colors duration-150 ${statusBadge(status)}`}
    >
      <span
        aria-hidden="true"
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDot(status)} ${isPulse ? 'animate-pulse pulse-ring' : ''}`}
      />
      {status}
    </span>
  )
}
