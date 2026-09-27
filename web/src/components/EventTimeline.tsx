import { formatTime } from '../utils'
import type { TaskEventLine } from '../hooks/useTaskEvents'

/** SSE 实时事件竖轴（生机绿贯穿轴；事件按类型着色） */
export default function EventTimeline({ timeline }: { timeline: TaskEventLine[] }) {
  if (timeline.length === 0) return null
  return (
    <div className="space-y-4 border-l-2 border-mint-100 ml-2 pl-4 text-xs">
      {timeline.map((ev, i) => {
        const dot = ev.kind === 'done' ? 'bg-mint-500 ring-mint-50'
          : ev.state === 'awaiting_approval' ? 'bg-amber-400 ring-amber-50'
          : 'bg-mint-400 ring-mint-50'
        return (
          <div key={i} className="relative">
            <span className={`absolute -left-[21px] top-0.5 w-2.5 h-2.5 rounded-full ring-4 ${dot}`} />
            <div className="font-bold text-slate-700">
              {formatTime(ev.at)} {ev.kind === 'done' ? '✓ 任务终态' : ev.kind === 'callback' ? 'worker 回调' : '状态更新'}
              {ev.snapshot && <span className="ml-1.5 text-slate-400 font-normal">快照</span>}
            </div>
            <div className="text-slate-400 mt-0.5">
              {ev.state && <span className="mr-2">state={ev.state}</span>}
              {ev.summary && <span title={ev.summary}>{ev.summary.length > 90 ? ev.summary.slice(0, 90) + '…' : ev.summary}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
