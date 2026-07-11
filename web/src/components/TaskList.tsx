import { useState } from 'react'
import type { TaskRecord } from '../types'
import { timeAgo } from '../utils'
import StatusBadge from './StatusBadge'
import TaskDetail from './TaskDetail'

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
            className="w-full text-left p-2 rounded hover:bg-white flex items-center gap-2"
          >
            <StatusBadge status={t.status} />
            <span className="font-mono text-xs text-slate-500">{t.task_id}</span>
            <span className="text-sm text-slate-700 truncate flex-1">{t.prompt}</span>
            <span className="text-xs text-slate-400 shrink-0">{timeAgo(t.updated_at)}</span>
          </button>
        ))}
      </div>
      {selected && (
        <TaskDetail
          task={selected}
          onClose={() => setSelected(null)}
          onChange={onChange}
        />
      )}
    </div>
  )
}
