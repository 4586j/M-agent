import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import StatusBadge from './StatusBadge'

describe('StatusBadge', () => {
  it('状态色全局唯一语义：awaiting_approval 必须 amber（设计文档 §5）', () => {
    render(<StatusBadge status="awaiting_approval" />)
    expect(screen.getByText('awaiting_approval')).toHaveClass('bg-amber-100')
  })

  it('done 用 mint 品牌色', () => {
    render(<StatusBadge status="done" />)
    expect(screen.getByText('done')).toHaveClass('bg-mint-100')
  })

  it('failed 用 rose 危险色', () => {
    render(<StatusBadge status="failed" />)
    expect(screen.getByText('failed')).toHaveClass('bg-rose-100')
  })

  it('未知状态兜底 slate 不崩溃', () => {
    render(<StatusBadge status="future-state" />)
    expect(screen.getByText('future-state')).toHaveClass('bg-slate-100')
  })
})
