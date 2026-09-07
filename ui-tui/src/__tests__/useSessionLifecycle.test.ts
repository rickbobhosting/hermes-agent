import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import {
  hydrateLiveSessionInflight,
  liveSessionInflightMessages,
  scheduleResumeScrollToBottom,
  signalFreshSessionBoundary,
  writeActiveSessionFile
} from '../app/useSessionLifecycle.js'
import {
  sessionActivateBreadcrumbKey,
  sessionCreateBreadcrumbKey,
  sessionResumeBreadcrumbKey
} from '../lib/activeSessionFile.js'

describe('fresh session boundary', () => {
  it('signals only when a live session is replaced by a different session', () => {
    const onFreshSessionStarted = vi.fn()

    expect(signalFreshSessionBoundary('old-session', 'new-session', onFreshSessionStarted)).toBe(true)
    expect(signalFreshSessionBoundary(null, 'first-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('same-session', 'same-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', null, onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', 'new-session')).toBe(false)
    expect(onFreshSessionStarted).toHaveBeenCalledOnce()
    expect(onFreshSessionStarted).toHaveBeenCalledWith('new-session')
  })
})

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })

  it('prefers the persistent create id over the gateway runtime id', () => {
    expect(
      sessionCreateBreadcrumbKey({ session_id: 'runtime01', stored_session_id: '20260906_persisted' })
    ).toBe('20260906_persisted')
    expect(sessionCreateBreadcrumbKey({ session_id: 'runtime01' })).toBe('runtime01')
  })

  it('uses persistent keys for activate and resume focus transitions', () => {
    const base = { messages: [], session_id: 'runtime01' }

    expect(sessionActivateBreadcrumbKey({ ...base, session_key: 'activated-stored' })).toBe('activated-stored')
    expect(sessionResumeBreadcrumbKey({ ...base, resumed: 'resumed-stored' })).toBe('resumed-stored')
    expect(sessionResumeBreadcrumbKey({ ...base, resumed: 'old', session_key: 'latest-stored' })).toBe('latest-stored')
  })
})

describe('live session activation in-flight state', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ streaming: true })
  })

  it('keeps the in-flight user prompt in history and hydrates partial assistant text', () => {
    const inflight = { assistant: 'partial answer', streaming: true, user: 'write a long answer' }

    expect(liveSessionInflightMessages(inflight)).toEqual([{ role: 'user', text: 'write a long answer' }])

    hydrateLiveSessionInflight(inflight)

    expect(turnController.bufRef).toBe('partial answer')
    expect(getTurnState().streaming).toBe('partial answer')
  })

  it('ignores empty in-flight payloads', () => {
    expect(liveSessionInflightMessages({ assistant: '', streaming: false, user: '   ' })).toEqual([])

    hydrateLiveSessionInflight({ assistant: '', streaming: false, user: '' })

    expect(turnController.bufRef).toBe('')
    expect(getTurnState().streaming).toBe('')
  })
})

describe('resume scroll settle', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('re-snaps while sticky and stops when the user scrolls away', () => {
    vi.useFakeTimers()
    let sticky = true
    let lastManualScrollAt = 0
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => lastManualScrollAt,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80, 240]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    sticky = false
    lastManualScrollAt = Date.now() + 1
    vi.advanceTimersByTime(160)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    cancel()
  })

  it('cancels pending resume snaps', () => {
    vi.useFakeTimers()
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => true,
          scrollToBottom
        }
      } as any,
      [20]
    )

    cancel()
    vi.advanceTimersByTime(20)

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the immediate resume snap even before sticky state settles', () => {
    vi.useFakeTimers()
    let sticky = false
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    sticky = true
    cancel()
  })
})
