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
  if (tasks.length === 0) return <div className="text-sm text-slate-400">该节点暂无任务</div>
  return (
    <div>
      <div className="space-y-1">
        {tasks.map((t) => (
          <button
            key={t.task_id}
            onClick={() => setSelected(t)}
            className="w-full text-left p-2 rounded-lg hover:bg-mint-50/60 flex items-center gap-2 transition"
          >
            <span className={`w-2 h-2 rounded-full shrink-0 ${statusDot(t.status)}`} />
            <StatusBadge status={t.status} />
            {t.verdict?.verdict === 'block' && (
              <span className="text-amber-600 text-xs" title={t.verdict.reason}>⚠</span>
            )}
            <span className="font-mono text-xs text-slate-500">{t.task_id}</span>
            <span className="text-sm text-slate-700 truncate flex-1">{t.prompt}</span>
            {t.meta?.cost_usd != null && (
              <span className="text-xs text-mint-700 shrink-0">{fmtUsd(t.meta.cost_usd)}</span>
            )}
            <span className="text-xs text-slate-400 shrink-0">{timeAgo(t.updated_at)}</span>
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
