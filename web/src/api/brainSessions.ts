import type { Connection } from './connections'

const LEGACY_KEY = 'orch_dispatch_session_id'

/** The endpoint is part of the scope so editing an existing connection cannot reuse its old session. */
export function sessionSelectionKey(connection: Pick<Connection, 'id' | 'baseUrl'>): string {
  return `${LEGACY_KEY}:${encodeURIComponent(connection.id)}:${encodeURIComponent(connection.baseUrl.trim().replace(/\/+$/, ''))}`
}

export function readSessionSelection(key: string): string {
  try { return localStorage.getItem(key) || '' } catch { return '' }
}

export function saveSessionSelection(key: string, id: string): void {
  try {
    if (id) localStorage.setItem(key, id)
    else localStorage.removeItem(key)
    localStorage.removeItem(LEGACY_KEY)
  } catch { /* Storage unavailable: the current form keeps its in-memory choice. */ }
}

/** Migrate the unscoped legacy choice only after this Brain confirms it owns that session. */
export function migrateSessionSelection(key: string, knownIds: string[]): string {
  const selected = readSessionSelection(key)
  if (selected) return knownIds.includes(selected) ? selected : ''
  try {
    const legacy = localStorage.getItem(LEGACY_KEY) || ''
    if (legacy && knownIds.includes(legacy)) {
      saveSessionSelection(key, legacy)
      return legacy
    }
  } catch { /* No persistent storage. */ }
  return ''
}
