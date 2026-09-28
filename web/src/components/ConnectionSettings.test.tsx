import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ConnectionSettings from './ConnectionSettings'
import {
  createConnection,
  saveConnection,
  deleteConnection,
  getConnections,
  getActiveConnectionId,
} from '../api/connections'

/** 连接管理器 UI：新增/保存并切换/测试连接/删除。连接单例状态跨测试共享，故测试前清掉非默认连接。 */

beforeEach(() => {
  for (const c of getConnections()) {
    if (c.id !== 'default') deleteConnection(c.id)
  }
  vi.stubGlobal('confirm', vi.fn(() => true))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function open() {
  render(<ConnectionSettings />)
  fireEvent.click(screen.getByRole('button', { name: /连接设置/ }))
}

describe('ConnectionSettings 连接管理器', () => {
  it('初始显示默认连接，新增并保存后自动切换', () => {
    open()
    expect(screen.getByText('默认')).toBeTruthy()

    fireEvent.click(screen.getByText('＋ 新增连接'))
    fireEvent.change(screen.getByPlaceholderText(/名称/), { target: { value: '第二大脑' } })
    fireEvent.change(screen.getByPlaceholderText(/地址/), {
      target: { value: 'http://127.0.0.1:7001/' },
    })

    fireEvent.click(screen.getByText('保存并切换'))
    expect(screen.getAllByText('第二大脑').length).toBeGreaterThan(0)
    expect(screen.queryByText('保存并切换')).toBeNull() // 表单收起
    expect(screen.getByText('http://127.0.0.1:7001')).toBeTruthy() // 去尾部斜杠
  })

  it('点击既有连接切换到它', () => {
    const conn = createConnection('另一脑', 'http://127.0.0.1:7009', '')
    saveConnection(conn)
    open()

    fireEvent.click(screen.getAllByText('另一脑')[0])
    expect(getActiveConnectionId()).toBe(conn.id)
    // 触发按钮 title 反映当前活动连接
    expect(screen.getByRole('button', { name: /连接设置/ }).title).toContain('另一脑')
  })

  it('测试连接：200 显示「连接正常」', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    open()
    fireEvent.click(screen.getByText('＋ 新增连接'))
    fireEvent.change(screen.getByPlaceholderText(/地址/), {
      target: { value: 'http://127.0.0.1:7001' },
    })
    fireEvent.click(screen.getByText('测试连接'))
    await waitFor(() => expect(screen.getByText('连接正常')).toBeTruthy())
  })

  it('测试连接：401 显示鉴权失败（区别于网络错误）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }))
    open()
    fireEvent.click(screen.getByText('＋ 新增连接'))
    fireEvent.change(screen.getByPlaceholderText(/地址/), {
      target: { value: 'http://127.0.0.1:7001' },
    })
    fireEvent.click(screen.getByText('测试连接'))
    await waitFor(() => expect(screen.getByText(/API Key 鉴权失败/)).toBeTruthy())
  })

  it('删除连接需确认，确认后从列表移除', () => {
    const conn = createConnection('临时脑', 'http://127.0.0.1:7011', '')
    saveConnection(conn)
    open()

    fireEvent.click(screen.getAllByRole('button', { name: /删除 临时脑/ })[0])
    expect(screen.queryByText('http://127.0.0.1:7011')).toBeNull()
  })
})