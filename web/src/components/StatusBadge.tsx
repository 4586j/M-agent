// 状态色全局唯一语义（docs/客户端界面设计.md §5）：
// done=Emerald · running/pending=Sky/Slate · awaiting_approval/warn=Amber · failed/block=Rose
const COLORS: Record<string, string> = {
  pending: 'bg-slate-100 text-slate-600',
  running: 'bg-sky-100 text-sky-700',
  done: 'bg-mint-100 text-mint-700',
  failed: 'bg-rose-100 text-rose-700',
  lost: 'bg-slate-200 text-slate-600',
  cancelled: 'bg-zinc-200 text-zinc-600',
  awaiting_approval: 'bg-amber-100 text-amber-700',
}

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium shrink-0 ${COLORS[status] || 'bg-slate-100 text-slate-600'}`}>
      {status}
    </span>
  )
}
