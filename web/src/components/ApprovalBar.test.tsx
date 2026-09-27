import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import ApprovalBar from './ApprovalBar'
import type { TaskRecord } from '../types'

vi.mock('../api/client', () => ({
  api: {
    approveTask: vi.fn(),
    rejectTask: vi.fn(),
  },
}))

import { api } from '../api/client'
const approveMock = vi.mocked(api.approveTask)
const rejectMock = vi.mocked(api.rejectTask)

function makeTask(overrides: Partial<TaskRecord> = {}): TaskRecord {
  return {
    task_id: 'task-a1', role: 'web-expert',
    prompt: '原始指令：实现登录页',
    worker_id: 'w1', status: 'awaiting_approval',
    created_at: '100', updated_at: '100',
    output: null, artifacts: [], error: null,
    meta: {
      approval: {
        status: 'pending',
        verdict: {
          verdict: 'block', reason: '检测到越权路径访问',
          matched_rules: ['R-PATH-01'], suggestion: '把改动限定在 output/ 下',
        },
        original_prompt: '原始指令：实现登录页',
      },
    },
    ...overrides,
  }
}

beforeEach(() => {
  approveMock.mockReset()
  rejectMock.mockReset()
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => vi.unstubAllGlobals())

describe('ApprovalBar', () => {
  it('渲染裁决理由、命中规则、原指令与操作按钮', () => {
    render(<ApprovalBar task={makeTask()} />)
    expect(screen.getByText(/检测到越权路径访问/)).toBeInTheDocument()
    expect(screen.getByText(/R-PATH-01/)).toBeInTheDocument()
    expect(screen.getByText('原始指令：实现登录页')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '放行' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝任务' })).toBeInTheDocument()
  })

  it('无修订直接放行成功：显示新任务 id 并通知刷新', async () => {
    approveMock.mockResolvedValue({ approved: true, dispatched_task_id: 'task-new', status: 'done', task_id: 'task-new', output: null, artifacts: [], error: null })
    const onDone = vi.fn()
    render(<ApprovalBar task={makeTask()} onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: '放行' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(screen.getByText(/task-new/)).toBeInTheDocument()
    expect(approveMock).toHaveBeenCalledWith('task-a1', undefined) // 空 prompt → 原样放行
  })

  it('修订后放行：把修订内容作为 prompt 传给后端', async () => {
    approveMock.mockResolvedValue({ approved: true, status: 'done', task_id: 'task-new2', output: null, artifacts: [], error: null })
    render(<ApprovalBar task={makeTask()} />)
    fireEvent.change(screen.getByPlaceholderText(/可附加放行约束/), {
      target: { value: '仅允许写 output/ 目录' },
    })
    fireEvent.click(screen.getByRole('button', { name: '修订并放行' }))
    await waitFor(() => expect(approveMock).toHaveBeenCalledWith('task-a1', '仅允许写 output/ 目录'))
  })

  it('放行未过检（approved=false）：显示失败原因，保留修订框', async () => {
    approveMock.mockResolvedValue({
      approved: false, status: 'failed', error: '修订后的指令仍未通过安全检验',
      task_id: '', output: null, artifacts: [],
    })
    render(<ApprovalBar task={makeTask()} />)
    fireEvent.click(screen.getByRole('button', { name: '放行' }))
    await waitFor(() => expect(screen.getByText(/仍未通过安全检验/)).toBeInTheDocument())
    expect(screen.getByPlaceholderText(/可附加放行约束/)).toBeInTheDocument() // 未清空，可重试
  })

  it('拒绝：走 confirm 后调用 rejectTask', async () => {
    rejectMock.mockResolvedValue({ task_id: 'task-a1', role: 'web-expert', status: 'failed', prompt: '', worker_id: null, created_at: '', updated_at: '', output: null, artifacts: [], error: null })
    const onDone = vi.fn()
    render(<ApprovalBar task={makeTask()} onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: '拒绝任务' }))
    await waitFor(() => expect(rejectMock).toHaveBeenCalledWith('task-a1'))
    expect(onDone).toHaveBeenCalled()
  })

  it('拒绝被取消（confirm=false）时不调用后端', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    render(<ApprovalBar task={makeTask()} />)
    fireEvent.click(screen.getByRole('button', { name: '拒绝任务' }))
    expect(rejectMock).not.toHaveBeenCalled()
  })
})
