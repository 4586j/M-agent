import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import NodeManage, { panelUrl } from './NodeManage'
import type { WorkerInfo } from '../types'

/** NodeManage 节点面板入口：行点击新标签打开 / 内嵌抽屉 iframe / host 环回回退。
 * 轮询数据 api.workers / api.tasks 全部 mock，不触网、不依赖后端。 */

const WORKERS: WorkerInfo[] = [
  { worker_id: 'worker-a', role: 'executor', host: '127.0.0.1', port: 8001,
    capabilities: {}, last_heartbeat: 0, load: 1 },
  { worker_id: 'worker-b', role: 'executor', host: '0.0.0.0', port: 8002,
    capabilities: {}, last_heartbeat: 0, load: 2 },
]

vi.mock('../api/client', () => ({
  api: {
    workers: vi.fn(() => Promise.resolve(WORKERS)),
    tasks: vi.fn(() => Promise.resolve([])),
    heartbeat: vi.fn(() => Promise.resolve({ status: 'ok' })),
    deleteWorker: vi.fn(() => Promise.resolve({ status: 'ok' })),
    register: vi.fn(() => Promise.resolve({ status: 'ok' })),
  },
}))

beforeEach(() => {
  vi.spyOn(window, 'open').mockImplementation(() => null)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('panelUrl · host 环回回退', () => {
  it('0.0.0.0 / :: 回退 127.0.0.1，其余 host 原样', () => {
    const w = (host: string, port: number): WorkerInfo => (
      { worker_id: 'w', role: 'executor', host, port, capabilities: {}, last_heartbeat: 0, load: 0 })
    expect(panelUrl(w('127.0.0.1', 8001))).toBe('http://127.0.0.1:8001/')
    expect(panelUrl(w('0.0.0.0', 8002))).toBe('http://127.0.0.1:8002/')
    expect(panelUrl(w('::', 8003))).toBe('http://127.0.0.1:8003/')
    expect(panelUrl(w('192.168.1.5', 8001))).toBe('http://192.168.1.5:8001/')
  })
})

describe('NodeManage 节点面板入口', () => {
  it('渲染节点行，点击行用该节点面板地址新标签打开', async () => {
    render(<MemoryRouter><NodeManage /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('worker-a')).toBeTruthy())

    fireEvent.click(screen.getByText('worker-a'))
    expect(window.open).toHaveBeenCalledWith('http://127.0.0.1:8001/', '_blank', 'noopener')
  })

  it('0.0.0.0 host 的行点击回退 127.0.0.1', async () => {
    render(<MemoryRouter><NodeManage /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('worker-b')).toBeTruthy())

    fireEvent.click(screen.getByText('worker-b'))
    expect(window.open).toHaveBeenCalledWith('http://127.0.0.1:8002/', '_blank', 'noopener')
  })

  it('「内嵌查看」打开抽屉 iframe，关闭后消失', async () => {
    render(<MemoryRouter><NodeManage /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('worker-a')).toBeTruthy())

    fireEvent.click(screen.getAllByRole('button', { name: /内嵌查看/ })[0])
    const frame = screen.getByTitle('节点 Agent 面板 worker-a')
    expect(frame.getAttribute('src')).toBe('http://127.0.0.1:8001/')

    fireEvent.click(screen.getByRole('button', { name: /✕ 关闭/ }))
    await waitFor(() => expect(screen.queryByTitle('节点 Agent 面板 worker-a')).toBeNull())
  })

  it('内嵌抽屉内点击行内按钮不冒泡触发新标签打开', async () => {
    render(<MemoryRouter><NodeManage /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('worker-a')).toBeTruthy())

    // 点击「刷新心跳」只走心跳，不触发行级 window.open
    fireEvent.click(screen.getAllByRole('button', { name: '刷新心跳' })[0])
    expect(window.open).not.toHaveBeenCalled()
  })
})