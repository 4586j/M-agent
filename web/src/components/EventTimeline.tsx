import { formatTime } from '../utils'
import type { TaskEventLine } from '../hooks/useTaskEvents'

/** SSE 实时事件竖轴（生机绿贯穿轴；事件按类型着色） */
export default function EventTimeline({ timeline }: { timeline: TaskEventLine[] }) {
  if (timeline.length === 0) return null
  return (
    <div className="space-y-4 border-l border-zinc-800 ml-3 pl-5 text-xs font-mono">
      {timeline.map((ev, i) => {
        const isLatest = i === timeline.length - 1
        const dot = ev.kind === 'done'
          ? 'bg-emerald-400 ring-4 ring-emerald-500/20 shadow-[0_0_8px_rgba(52,211,153,0.5)]'
          : ev.state === 'awaiting_approval'
          ? 'bg-amber-400 ring-4 ring-amber-500/20 shadow-[0_0_8px_rgba(251,191,36,0.5)]'
          : isLatest
          ? 'bg-emerald-400 ring-4 ring-emerald-500/20 animate-pulse'
          : 'bg-zinc-600 ring-4 ring-zinc-800'
        return (
          <div key={i} className="relative">
            <span className={`absolute -left-[25px] top-1 w-2 h-2 rounded-full ${dot}`} />
            <div className="font-semibold text-zinc-200 tabular-nums">
              {formatTime(ev.at)} {ev.kind === 'done' ? '✓ 任务终态' : ev.kind === 'callback' ? 'worker 回调' : '状态更新'}
              {ev.snapshot && <span className="ml-1.5 text-zinc-500 font-normal">快照</span>}
            </div>
            <div className="text-zinc-400 mt-0.5">
              {ev.state && <span className="mr-2 text-zinc-500">state={ev.state}</span>}
              {ev.summary && <span title={ev.summary}>{ev.summary.length > 90 ? ev.summary.slice(0, 90) + '…' : ev.summary}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
