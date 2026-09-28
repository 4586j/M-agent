// 轻量 cron 预览（仅前端展示用，以后端 compute_next_run 校验为准）。
// 支持标准 5 段：分 时 日 月 周；字段支持 星号、数字、a-b、星号/步长、a-b/步长、列表 a,b,c。

// 每字段的合法取值范围：分/时/周 0 基，日/月 1 基（标准 cron）
const RANGES: [number, number][] = [
  [0, 59], // 分钟
  [0, 23], // 小时
  [1, 31], // 日
  [1, 12], // 月
  [0, 6], // 周（0=周日）
]

function parseField(field: string, min: number, max: number): Set<number> | null {
  if (!/^[\d*,/\-]+$/.test(field)) return null
  const out = new Set<number>()
  for (const part of field.split(',')) {
    const [rangePart, stepPart] = part.split('/')
    const step = stepPart ? parseInt(stepPart, 10) : 1
    if (!Number.isInteger(step) || step <= 0) return null
    let lo = min
    let hi = max
    if (rangePart !== '*') {
      const m = rangePart.match(/^(\d+)(?:-(\d+))?$/)
      if (!m) return null
      lo = parseInt(m[1], 10)
      hi = m[2] != null ? parseInt(m[2], 10) : lo
      if (lo < min || hi > max || lo > hi) return null
    }
    for (let v = lo; v <= hi; v += step) out.add(v)
  }
  return out.size > 0 ? out : null
}

export function isValidCron(expr: string): boolean {
  const fields = expr.trim().split(/\s+/)
  if (fields.length !== 5) return false
  return fields.every((f, i) => parseField(f, RANGES[i][0], RANGES[i][1]) !== null)
}

/** 返回未来 count 次运行时间（epoch 秒）；表达式非法返回 []。按天跳进，上限两年。 */
export function nextRuns(expr: string, count = 3, from?: Date): number[] {
  const fields = expr.trim().split(/\s+/)
  if (fields.length !== 5) return []
  const sets = fields.map((f, i) => parseField(f, RANGES[i][0], RANGES[i][1]))
  if (sets.some((s) => s === null)) return []
  const [min, hour, dom, mon, dow] = sets as Set<number>[]
  const mins = [...min].sort((a, b) => a - b)
  const hours = [...hour].sort((a, b) => a - b)

  const start = from ?? new Date()
  const startMs = start.getTime()
  const domRestricted = fields[2] !== '*'
  const dowRestricted = fields[4] !== '*'

  const out: number[] = []
  // 按天跳进（上限两年），只在候选日扫描 小时×分钟 组合：旧实现逐分钟扫
  // （最坏 ~105 万次），对「当年无命中」的表达式（如 2 月 31 日）每次击键
  // 都白跑全量；按天过滤后，非命中日直接跳过。
  for (let day = 0; day < 2 * 366 && out.length < count; day++) {
    const cur = new Date(start)
    cur.setHours(0, 0, 0, 0)
    cur.setDate(cur.getDate() + day)
    // 日期级过滤：月/日/周任一不满足则整天跳过
    if (!mon.has(cur.getMonth() + 1)) continue
    const domOk = dom.has(cur.getDate())
    const dowOk = dow.has(cur.getDay())
    // 标准 cron 语义：日与周都受限时取或，否则取与
    const dayOk = domRestricted && dowRestricted ? (domOk || dowOk) : (domOk && dowOk)
    if (!dayOk) continue
    for (const h of hours) {
      for (const m of mins) {
        const ts = new Date(cur)
        ts.setHours(h, m, 0, 0)
        if (ts.getTime() <= startMs) continue
        out.push(Math.floor(ts.getTime() / 1000))
        if (out.length >= count) break
      }
      if (out.length >= count) break
    }
  }
  return out
}
