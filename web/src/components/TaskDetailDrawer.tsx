import { useEffect, useState, type ReactNode } from 'react'
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

  // 全局 Escape 键监听，丝滑收起抽屉
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && open) onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

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
    <>
      {/* 半透明毛玻璃背景遮罩，点击丝滑关闭 */}
      <div
        className={`fixed inset-0 bg-black/60 backdrop-blur-sm z-40 transition-opacity duration-300 ${
          open ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
        }`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className={`fixed top-0 right-0 h-full w-full sm:w-[520px] lg:w-[42%] bg-zinc-950/95 backdrop-blur-2xl shadow-2xl border-l border-white/[0.08] z-50 transform transition-transform duration-300 ease-spring flex flex-col ${
          open ? 'translate-x-0' : 'translate-x-full'
        }`}
        role="dialog"
        aria-label="任务详情"
      >
      {/* 头部 */}
      <div className="p-5 border-b border-white/[0.07] flex items-center justify-between bg-zinc-900/40 shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <StatusBadge status={current.status} />
            <span className="font-mono text-sm font-semibold text-zinc-100 tracking-tight">{current.task_id}</span>
          </div>
          <p className="text-xs text-zinc-400 mt-1 font-mono">
            派发于 {timeAgo(current.created_at)} · 更新 {timeAgo(current.updated_at)}
            {channel !== 'off' && (
              <span className={`ml-2 font-medium ${channel === 'sse' ? 'text-emerald-400' : 'text-amber-400'}`}>
                {channel === 'sse' ? '● SSE 实时' : '○ 已降级轮询'}
              </span>
            )}
          </p>
        </div>
        <button onClick={onClose} aria-label="关闭" className="p-1.5 rounded-lg hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 transition">
          ✕
        </button>
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto p-5 space-y-5 custom-scroll text-zinc-300">
        {current.status === 'awaiting_approval' && (
          <ApprovalBar task={current} onDone={onChange} />
        )}

        <Section title="任务 prompt">
          <pre className="bg-[#050507] border border-white/[0.06] rounded-xl p-3 text-xs font-mono whitespace-pre-wrap text-zinc-200">{current.prompt}</pre>
        </Section>
        {current.output != null && (
          <Section title="输出">
            <pre className="bg-[#050507] border border-white/[0.06] rounded-xl p-3 text-xs font-mono whitespace-pre-wrap max-h-60 overflow-auto text-emerald-300/90 custom-scroll">{String(current.output)}</pre>
          </Section>
        )}
        {current.error != null && (
          <Section title="错误">
            <pre className="bg-rose-950/30 border border-rose-500/20 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap text-rose-300">{String(current.error)}</pre>
          </Section>
        )}
        {current.artifacts.length > 0 && (
          <Section title="产物文件（点击预览）">
            <div className="flex flex-wrap gap-2 mb-2">
              {current.artifacts.map((p) => (
                <button
                  key={p}
                  onClick={() => loadArtifact(p)}
                  className="px-2.5 py-1 text-xs rounded-lg bg-zinc-800/80 text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/10 font-mono transition"
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
            <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-3.5 backdrop-blur-md">
              <div className="text-amber-300 font-semibold text-sm mb-1 flex items-center gap-1.5">
                <span>⚠ 产物被标记</span>
              </div>
              <div className="text-xs text-amber-200/90 mb-1 leading-relaxed">{current.verdict.reason}</div>
              {current.verdict.matched_rules.length > 0 && (
                <div className="text-xs text-zinc-400 mb-1 font-mono">命中: {current.verdict.matched_rules.join(', ')}</div>
              )}
              {current.verdict.suggestion && (
                <div className="text-xs text-zinc-300">建议: {current.verdict.suggestion}</div>
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
      <div className="border-t border-white/[0.07] p-4 flex items-center gap-2 bg-zinc-900/60 shrink-0">
        <button
          disabled={busy || !cancellable}
          onClick={() => act(() => api.cancelTask(task.task_id), '取消')}
          className="btn-press px-3.5 py-1.5 text-xs font-medium rounded-lg bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 border border-amber-500/20 disabled:opacity-40 transition font-mono"
        >
          取消任务
        </button>
        <button
          disabled={busy}
          onClick={() => act(() => api.redispatchTask(task.task_id), '重派')}
          className="btn-press px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-sky-500/10 text-sky-300 hover:bg-sky-500/20 border border-sky-500/20 disabled:opacity-40 transition font-mono"
        >
          重派任务
        </button>
        {msg && <span className="text-xs text-zinc-400 font-mono ml-auto">{msg}</span>}
      </div>
    </aside>
    </>
  )
}

const EMPTY_TASK: TaskRecord = {
  task_id: '', role: '', prompt: '', worker_id: null, status: 'pending',
  created_at: '', updated_at: '', output: null, artifacts: [], error: null,
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider mb-2">{title}</div>
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
          className="max-h-60 rounded-xl border border-white/[0.08]"
        />
      )
    }
    return (
      <div className="text-xs text-zinc-400 bg-zinc-900 border border-white/[0.06] rounded-xl p-3 font-mono">
        二进制文件（{artifact.size ?? '?'} 字节），不支持文本预览
      </div>
    )
  }
  return (
    <pre className="bg-[#050507] border border-white/[0.06] text-zinc-200 rounded-xl p-3 text-xs font-mono whitespace-pre-wrap max-h-60 overflow-auto custom-scroll">{artifact.content}</pre>
  )
}
