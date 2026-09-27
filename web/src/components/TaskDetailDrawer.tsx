import { useState, type ReactNode } from 'react'
import type { TaskRecord } from '../types'
import { api } from '../api/client'
import { timeAgo } from '../utils'
import StatusBadge from './StatusBadge'
import { useTaskEvents } from '../hooks/useTaskEvents'
import EventTimeline from './EventTimeline'
import UsageCard from './UsageCard'
import ApprovalBar from './ApprovalBar'

/**
 * 任务详情抽屉（右侧 40%，生机绿白规格）：
 * 审批条置顶 → prompt/输出/错误 → 产物预览 → 产物裁决 → 用量 → SSE 时间线 → 操作。
 * SSE 订阅经 useTaskEvents；小屏（<640px）折叠为全屏页。
 */
export default function TaskDetailDrawer({
  task,
  open,
  onClose,
  onChange,
}: {
  task: TaskRecord | null
  open: boolean
  onClose: () => void
  onChange?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [artifact, setArtifact] = useState<{
    path: string; content: string; encoding?: string; size?: number
  } | null>(null)

  // task 为 null 时 hook 仍需稳定调用：用哨兵记录 + open 门控在渲染层
  const { current, timeline, channel } = useTaskEvents(
    task ?? EMPTY_TASK,
    onChange,
  )
  if (!task) return null
  const cancellable = current.status === 'pending' || current.status === 'running'

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
      setArtifact({ path, content: r.content, encoding: r.encoding, size: r.size })
    } catch (e) {
      setArtifact({ path, content: `加载失败: ${e instanceof Error ? e.message : String(e)}` })
    }
  }

  return (
    <aside
      className={`fixed top-0 right-0 h-full w-full sm:w-[480px] lg:w-[40%] bg-white shadow-2xl border-l border-mint-100 z-50 transform transition-transform duration-300 flex flex-col ${
        open ? 'translate-x-0' : 'translate-x-full'
      }`}
      role="dialog"
      aria-label="任务详情"
    >
      {/* 头部 */}
      <div className="p-5 border-b border-slate-100 flex items-center justify-between bg-mint-50/40 shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <StatusBadge status={current.status} />
            <span className="font-mono text-sm font-semibold text-slate-800">{current.task_id}</span>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            派发于 {timeAgo(current.created_at)} · 更新 {timeAgo(current.updated_at)}
            {channel !== 'off' && (
              <span className={`ml-2 font-medium ${channel === 'sse' ? 'text-mint-600' : 'text-amber-600'}`}>
                {channel === 'sse' ? '● SSE 实时' : '○ 已降级轮询'}
              </span>
            )}
          </p>
        </div>
        <button onClick={onClose} aria-label="关闭" className="p-1.5 rounded-lg hover:bg-white text-slate-400 hover:text-slate-600 transition">
          ✕
        </button>
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto p-5 space-y-5 custom-scroll">
        {current.status === 'awaiting_approval' && (
          <ApprovalBar task={current} onDone={onChange} />
        )}

        <Section title="任务 prompt">
          <pre className="bg-slate-50 rounded-lg p-2.5 text-sm whitespace-pre-wrap">{current.prompt}</pre>
        </Section>
        {current.output != null && (
          <Section title="输出">
            <pre className="bg-slate-50 rounded-lg p-2.5 text-sm whitespace-pre-wrap max-h-60 overflow-auto">{String(current.output)}</pre>
          </Section>
        )}
        {current.error != null && (
          <Section title="错误">
            <pre className="bg-rose-50 rounded-lg p-2.5 text-sm whitespace-pre-wrap">{String(current.error)}</pre>
          </Section>
        )}
        {current.artifacts.length > 0 && (
          <Section title="产物文件（点击预览）">
            <div className="flex flex-wrap gap-2 mb-2">
              {current.artifacts.map((p) => (
                <button
                  key={p}
                  onClick={() => loadArtifact(p)}
                  className="px-2 py-1 text-xs rounded-lg bg-mint-50 text-mint-700 hover:bg-mint-100 font-mono transition"
                >
                  {p}
                </button>
              ))}
            </div>
            {artifact && <ArtifactView artifact={artifact} />}
          </Section>
        )}
        {current.verdict?.verdict === 'block' && current.status !== 'awaiting_approval' && (
          <Section title="security-guard 产物裁决">
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-3">
              <div className="text-amber-700 font-semibold text-sm mb-1">⚠ 产物被标记</div>
              <div className="text-xs text-slate-700 mb-1">{current.verdict.reason}</div>
              {current.verdict.matched_rules.length > 0 && (
                <div className="text-xs text-slate-500 mb-1">命中: {current.verdict.matched_rules.join(', ')}</div>
              )}
              {current.verdict.suggestion && (
                <div className="text-xs text-slate-600">建议: {current.verdict.suggestion}</div>
              )}
            </div>
          </Section>
        )}

        <UsageCard meta={current.meta} />

        {timeline.length > 0 && (
          <Section title={`事件时间线（${channel === 'sse' ? 'SSE 已连接' : channel === 'poll' ? 'SSE 不可用，已降级轮询' : '已结束'}）`}>
            <EventTimeline timeline={timeline} />
          </Section>
        )}
      </div>

      {/* 底部操作 */}
      <div className="border-t border-slate-100 p-4 flex items-center gap-2 bg-white shrink-0">
        <button
          disabled={busy || !cancellable}
          onClick={() => act(() => api.cancelTask(task.task_id), '取消')}
          className="px-3 py-1.5 text-xs rounded-lg bg-amber-100 text-amber-700 hover:bg-amber-200 disabled:opacity-40 transition"
        >
          取消任务
        </button>
        <button
          disabled={busy}
          onClick={() => act(() => api.redispatchTask(task.task_id), '重派')}
          className="px-3 py-1.5 text-xs rounded-lg bg-sky-100 text-sky-700 hover:bg-sky-200 disabled:opacity-40 transition"
        >
          重派任务
        </button>
        {msg && <span className="text-xs text-slate-500 ml-auto">{msg}</span>}
      </div>
    </aside>
  )
}

const EMPTY_TASK: TaskRecord = {
  task_id: '', role: '', prompt: '', worker_id: null, status: 'pending',
  created_at: '', updated_at: '', output: null, artifacts: [], error: null,
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">{title}</div>
      {children}
    </div>
  )
}

const IMAGE_MIME: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
  gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
}

function ArtifactView({ artifact }: { artifact: { path: string; content: string; encoding?: string; size?: number } }) {
  if (artifact.encoding === 'base64') {
    const ext = artifact.path.split('.').pop()?.toLowerCase() ?? ''
    const mime = IMAGE_MIME[ext]
    if (mime) {
      return (
        <img
          src={`data:${mime};base64,${artifact.content}`}
          alt={artifact.path}
          className="max-h-60 rounded-lg border border-slate-200"
        />
      )
    }
    return (
      <div className="text-xs text-slate-500 bg-slate-50 rounded-lg p-2">
        二进制文件（{artifact.size ?? '?'} 字节），不支持文本预览
      </div>
    )
  }
  return (
    <pre className="bg-slate-900 text-slate-100 rounded-lg p-2.5 text-xs whitespace-pre-wrap max-h-60 overflow-auto">{artifact.content}</pre>
  )
}
