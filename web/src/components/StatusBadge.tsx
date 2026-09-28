import { statusBadge } from '../types'

/** 状态小徽章；色值语义单点见 types.ts STATUS_RENDER（docs/客户端界面设计.md §5） */
export default function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium shrink-0 ${statusBadge(status)}`}>
      {status}
    </span>
  )
}
