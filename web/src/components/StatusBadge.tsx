const COLORS: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-700',
  running: 'bg-sky-100 text-sky-700',
  done: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-rose-100 text-rose-700',
  lost: 'bg-slate-200 text-slate-600',
  cancelled: 'bg-zinc-200 text-zinc-600',
}

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${COLORS[status] || 'bg-slate-100 text-slate-600'}`}>
      {status}
    </span>
  )
}
