import { useState } from 'react'
import type { TaskRecord } from '../types'
import { statusDot } from '../types'
import { fmtUsd, timeAgo } from '../utils'
import StatusBadge from './StatusBadge'
import TaskDetailDrawer from './TaskDetailDrawer'

export default function TaskList({
  tasks,
  onChange,
}: {
  tasks: TaskRecord[]
  onChange?: () => void
}) {
  const [selected, setSelected] = useState<TaskRecord | null>(null)
  if (tasks.length === 0) return <div className="text-sm text-zinc-500 font-mono py-4 text-center">该节点暂无任务</div>
  return (
    <div>
      <div className="space-y-1">
        {tasks.map((t) => (
          <button
            key={t.task_id}
            onClick={() => setSelected(t)}
            className="w-full text-left p-2.5 rounded-xl hover:bg-zinc-800/50 border border-transparent hover:border-white/[0.06] flex items-center gap-2.5 transition duration-150 group"
          >
            <span className={`w-2 h-2 rounded-full shrink-0 ${statusDot(t.status)}`} />
            <StatusBadge status={t.status} />
            {t.verdict?.verdict === 'block' && (
              <span className="text-amber-400 text-xs" title={t.verdict.reason}>⚠</span>
            )}
            <span className="font-mono text-xs text-zinc-400 group-hover:text-emerald-400 transition shrink-0">{t.task_id}</span>
            <span className="text-sm text-zinc-300 group-hover:text-zinc-100 truncate flex-1 transition">{t.prompt}</span>
            {t.meta?.cost_usd != null && (
              <span className="text-xs text-emerald-400 font-mono tabular-nums shrink-0">{fmtUsd(t.meta.cost_usd)}</span>
            )}
            <span className="text-xs text-zinc-500 font-mono tabular-nums shrink-0">{timeAgo(t.updated_at)}</span>
          </button>
        ))}
      </div>
      <TaskDetailDrawer
        task={selected}
        open={selected != null}
        onClose={() => setSelected(null)}
        onChange={onChange}
      />
    </div>
  )
}
