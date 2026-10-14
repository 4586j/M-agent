import { statusBadge, statusDot } from '../types'

/** 状态小徽章；带状态圆点（Dot）的半透明胶囊徽章（深邃暗夜 + 翡翠薄荷） */
export default function StatusBadge({ status }: { status: string }) {
  const isPulse = status === 'running' || status === 'online'
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border border-white/[0.08] shrink-0 font-mono ${statusBadge(status)}`}
    >
      <span
        aria-hidden="true"
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDot(status)} ${isPulse ? 'animate-pulse' : ''}`}
      />
      {status}
    </span>
  )
}

