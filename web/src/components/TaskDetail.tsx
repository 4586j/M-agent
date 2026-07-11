import { useState, type ReactNode } from 'react'
import type { TaskRecord } from '../types'
import { api } from '../api/client'
import { timeAgo } from '../utils'
import StatusBadge from './StatusBadge'

export default function TaskDetail({
  task,
  onClose,
  onChange,
}: {
  task: TaskRecord
  onClose: () => void
  onChange?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [artifact, setArtifact] = useState<{ path: string; content: string } | null>(null)

  const act = async (fn: () => Promise<unknown>, label: string) => {
    setBusy(true)
    setMsg(null)
    try {
      await fn()
      setMsg(`${label} 完成`)
      onChange?.()
    } catch (e) {
      setMsg(`${label} 失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const loadArtifact = async (path: string) => {
    if (!task.worker_id) return
    try {
      const r = await api.artifact(task.worker_id, task.task_id, path)
      setArtifact({ path, content: r.content })
    } catch (e) {
      setArtifact({ path, content: `加载失败: ${e instanceof Error ? e.message : String(e)}` })
    }
  }

  const cancellable = task.status === 'pending' || task.status === 'running'

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-lg shadow-xl max-w-2xl w-full max-h-[85vh] overflow-auto p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <StatusBadge status={task.status} />
            <span className="font-mono text-sm">{task.task_id}</span>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div className="text-xs text-slate-500 mb-3">
          role: {task.role} · worker: {task.worker_id || '-'} · 创建 {timeAgo(task.created_at)} · 更新 {timeAgo(task.updated_at)}
        </div>
        <Section title="任务 prompt">
          <pre className="bg-slate-50 rounded p-2 text-sm whitespace-pre-wrap">{task.prompt}</pre>
        </Section>
        {task.output != null && (
          <Section title="输出">
            <pre className="bg-slate-50 rounded p-2 text-sm whitespace-pre-wrap max-h-60 overflow-auto">{String(task.output)}</pre>
          </Section>
        )}
        {task.error != null && (
          <Section title="错误">
            <pre className="bg-rose-50 rounded p-2 text-sm whitespace-pre-wrap">{String(task.error)}</pre>
          </Section>
        )}
        {task.artifacts.length > 0 && (
          <Section title="产物文件（点击预览）">
            <div className="flex flex-wrap gap-2 mb-2">
              {task.artifacts.map((p) => (
                <button
                  key={p}
                  onClick={() => loadArtifact(p)}
                  className="px-2 py-1 text-xs rounded bg-slate-100 hover:bg-slate-200 font-mono"
                >
                  {p}
                </button>
              ))}
            </div>
            {artifact && (
              <pre className="bg-slate-900 text-slate-100 rounded p-2 text-xs whitespace-pre-wrap max-h-60 overflow-auto">{artifact.content}</pre>
            )}
          </Section>
        )}
        <div className="flex gap-2 items-center mt-4">
          <button
            disabled={busy || !cancellable}
            onClick={() => act(() => api.cancelTask(task.task_id), '取消')}
            className="px-3 py-1 text-sm rounded bg-amber-100 text-amber-700 hover:bg-amber-200 disabled:opacity-40"
          >
            取消任务
          </button>
          <button
            disabled={busy}
            onClick={() => act(() => api.redispatchTask(task.task_id), '重派')}
            className="px-3 py-1 text-sm rounded bg-sky-100 text-sky-700 hover:bg-sky-200 disabled:opacity-40"
          >
            重派任务
          </button>
          {msg && <span className="text-xs text-slate-500">{msg}</span>}
        </div>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mb-3">
      <div className="text-xs text-slate-500 mb-1">{title}</div>
      {children}
    </div>
  )
}
