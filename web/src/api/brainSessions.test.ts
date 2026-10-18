import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { migrateSessionSelection, readSessionSelection, saveSessionSelection, sessionSelectionKey } from './brainSessions'

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('Brain session selections', () => {
  const a = sessionSelectionKey({ id: 'a', baseUrl: 'http://brain-a.test' })
  const b = sessionSelectionKey({ id: 'b', baseUrl: 'http://brain-b.test' })
  it('keeps each Brain selection independent', () => {
    saveSessionSelection(a, 'session-a')
    saveSessionSelection(b, 'session-b')
    expect(readSessionSelection(a)).toBe('session-a')
    expect(readSessionSelection(b)).toBe('session-b')
    saveSessionSelection(b, '')
    expect(readSessionSelection(a)).toBe('session-a')
    expect(readSessionSelection(b)).toBe('')
  })
  it('treats a changed endpoint as a new scope and normalizes trailing slashes', () => {
    saveSessionSelection(a, 'session-a')
    expect(sessionSelectionKey({ id:'a',baseUrl:' http://brain-a.test/ ' })).toBe(a)
    expect(readSessionSelection(sessionSelectionKey({id:'a',baseUrl:'http://brain-b.test'}))).toBe('')
  })
  it('migrates legacy choices only when the Brain lists that session', () => {
    localStorage.setItem('orch_dispatch_session_id', 'session-a')
    expect(migrateSessionSelection(b, ['session-b'])).toBe('')
    expect(readSessionSelection(b)).toBe('')
    expect(migrateSessionSelection(a, ['session-a'])).toBe('session-a')
    expect(localStorage.getItem('orch_dispatch_session_id')).toBeNull()
    expect(readSessionSelection(a)).toBe('session-a')
  })
  it('does not restore a deleted or unknown scoped session', () => {
    saveSessionSelection(a, 'deleted')
    expect(migrateSessionSelection(a, ['current'])).toBe('')
  })
  it('handles unavailable browser storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(readSessionSelection(a)).toBe('')
    expect(migrateSessionSelection(a, ['current'])).toBe('')
    expect(() => saveSessionSelection(a, 'current')).not.toThrow()
  })
})
