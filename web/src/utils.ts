/** 把后端时间戳（epoch 秒，字符串或数字）格式化为相对时间；无法解析时返回 '-'。 */
export function timeAgo(ts: string | number | null | undefined): string {
  if (ts == null || ts === '') return '-'
  const n = typeof ts === 'number' ? ts : parseFloat(ts)
  if (!isFinite(n) || n <= 0) return '-'
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - n))
  if (diff < 60) return `${diff}s 前`
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`
  return `${Math.floor(diff / 86400)}d 前`
}

/** epoch 秒 -> HH:MM:SS（"几点发布"）。无法解析返回 '-'。 */
export function formatTime(ts: string | number | null | undefined): string {
  if (ts == null || ts === '') return '-'
  const n = typeof ts === 'number' ? ts : parseFloat(ts)
  if (!isFinite(n) || n <= 0) return '-'
  return new Date(n * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

/** 美元成本格式化：统一 4 位小数（微成本可见性优先），null/undefined → '-'。全站单一口径。 */
export function fmtUsd(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return '-'
  return `$${v.toFixed(4)}`
}

/** 数量格式化：千/百万级缩写，避免长数字挤坏表格。null → '-'。 */
export function fmtNum(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return String(v)
}
