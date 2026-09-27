import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import UsageCard from './UsageCard'
import type { TaskMeta } from '../types'

describe('UsageCard', () => {
  it('渲染 tokens/turns/成本/缓存命中率', () => {
    const meta: TaskMeta = {
      model: 'haiku', num_turns: 3, cost_usd: 0.0127,
      usage: { input_tokens: 2000, output_tokens: 760, cache_read_input_tokens: 8000, cache_creation_input_tokens: 500 },
    }
    render(<UsageCard meta={meta} />)
    // tokens 总和 = 2000+760+8000+500 = 11260
    expect(screen.getByText('11,260')).toBeInTheDocument()
    expect(screen.getByText('3 轮')).toBeInTheDocument()
    expect(screen.getByText('$0.0127')).toBeInTheDocument()
    // 缓存命中率 = 8000 / (8000+2000+500) = 76.19% → 76%
    expect(screen.getByText('76%')).toBeInTheDocument()
    expect(screen.getByText('model: haiku')).toBeInTheDocument()
  })

  it('meta 为空对象（无任何有效数据）时不渲染卡片', () => {
    const { container } = render(<UsageCard meta={{}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('meta 为空时不渲染', () => {
    const { container } = render(<UsageCard meta={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('无缓存数据时命中率为 —（0 命中不算命中率）', () => {
    render(<UsageCard meta={{ usage: { input_tokens: 100, output_tokens: 50 } }} />)
    expect(screen.getByText('150')).toBeInTheDocument()
    // turns/成本/缓存命中率均为 —
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })
})
