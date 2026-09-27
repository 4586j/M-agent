import { describe, expect, it } from 'vitest'
import { isValidCron, nextRuns } from './cron'

describe('isValidCron', () => {
  it('合法表达式', () => {
    expect(isValidCron('0 9 * * 1-5')).toBe(true)
    expect(isValidCron('*/15 * * * *')).toBe(true)
    expect(isValidCron('0,30 9 * * *')).toBe(true)
    expect(isValidCron('5 4 * * 0')).toBe(true) // 周日
    expect(isValidCron('0 0 1 1 *')).toBe(true)
    expect(isValidCron('59 23 31 12 6')).toBe(true)
  })

  it('非法表达式', () => {
    expect(isValidCron('99 9 * * *')).toBe(false) // 分钟越界
    expect(isValidCron('0 25 * * *')).toBe(false) // 小时越界
    expect(isValidCron('0 9 * * 7')).toBe(false) // 周越界
    expect(isValidCron('0 9 * *')).toBe(false) // 缺段
    expect(isValidCron('0 9 * * * *')).toBe(false) // 多段
    expect(isValidCron('abc 9 * * *')).toBe(false) // 字母
    expect(isValidCron('*/0 * * * *')).toBe(false) // 步长为 0
  })
})

describe('nextRuns', () => {
  const base = new Date('2026-09-28T10:07:30') // 周一

  it('*/15 命中 15/30/45 分钟边界', () => {
    const runs = nextRuns('*/15 * * * *', 3, base)
    expect(runs).toHaveLength(3)
    const mins = runs.map((ts) => new Date(ts * 1000).getMinutes())
    expect(mins).toEqual([15, 30, 45])
  })

  it('0 9 * * 1-5 跳过周末', () => {
    // 周五 10:07 → 下一次是次日（周六被跳过）周一 09:00？周五当日 9 点已过，
    // 下一命中 = 下周一 09:00
    const friday = new Date('2026-10-02T10:07:00') // 周五
    const runs = nextRuns('0 9 * * 1-5', 1, friday)
    const d = new Date(runs[0] * 1000)
    expect(d.getHours()).toBe(9)
    expect(d.getDay()).toBe(1) // 周一
  })

  it('结果单调递增且不含当前时刻', () => {
    const runs = nextRuns('* * * * *', 3, base)
    expect(runs).toHaveLength(3)
    expect(runs[0]).toBeGreaterThan(Math.floor(base.getTime() / 1000))
    expect(runs[1] - runs[0]).toBe(60)
    expect(runs[2] - runs[1]).toBe(60)
  })

  it('日与周双受限取或语义（1 号或周一）', () => {
    // 2026-10-01 是周四且非 1 号匹配日……直接验证：1 号必命中（dom 受限）
    const runs = nextRuns('0 9 1 * 1', 4, new Date('2026-09-28T10:07:00'))
    const dates = runs.map((ts) => new Date(ts * 1000))
    // 每个命中都应满足：9 点整，且（1 号 或 周一）
    for (const d of dates) {
      expect(d.getHours()).toBe(9)
      expect(d.getMinutes()).toBe(0)
      const isDom = d.getDate() === 1
      const isMon = d.getDay() === 1
      expect(isDom || isMon).toBe(true)
    }
  })

  it('非法表达式返回空数组', () => {
    expect(nextRuns('99 9 * * *', 3, base)).toEqual([])
    expect(nextRuns('bad cron', 3, base)).toEqual([])
  })

  it('区间与步长组合 a-b/n', () => {
    const runs = nextRuns('10-50/20 9 * * *', 3, base) // 分钟 10,30,50
    const mins = runs.map((ts) => new Date(ts * 1000).getMinutes())
    // 10:07 之后依次命中 9 点档不存在 → 次日 9:10、9:30、9:50
    // 注意 base 是 10:07，当天 9 点已过 → 全部落次日
    expect(mins).toEqual([10, 30, 50])
  })
})
