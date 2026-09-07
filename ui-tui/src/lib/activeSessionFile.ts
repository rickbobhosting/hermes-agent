import { writeFileSync } from 'node:fs'

import type { GatewayClient } from '../gatewayClient.js'
import type {
  SessionActivateResponse,
  SessionCreateResponse,
  SessionResumeResponse
} from '../gatewayTypes.js'

const publishedKeys = new WeakMap<GatewayClient, string>()

export const sessionCreateBreadcrumbKey = (response: SessionCreateResponse) =>
  response.stored_session_id ?? response.session_id

export const sessionActivateBreadcrumbKey = (response: SessionActivateResponse) =>
  response.session_key ?? response.session_id

export const sessionResumeBreadcrumbKey = (response: SessionResumeResponse) =>
  response.session_key ?? response.resumed ?? response.session_id

export const writeActiveSessionFile = (
  sessionId: null | string,
  file = process.env.HERMES_TUI_ACTIVE_SESSION_FILE
) => {
  if (!file || !sessionId) {
    return false
  }

  try {
    writeFileSync(file, JSON.stringify({ session_id: sessionId }), { mode: 0o600 })

    return true
  } catch {
    // Best-effort shell epilogue hint only; never break live session changes.
    return false
  }
}

/** Persist focus before publishing it so the dashboard can authenticate it. */
export const publishDashboardActiveSession = (
  gw: GatewayClient,
  sessionKey: null | string,
  runtimeSessionId?: string
) => {
  const key = sessionKey?.trim()

  if (!key) {
    return
  }

  if (!writeActiveSessionFile(key)) {
    return
  }

  if (publishedKeys.get(gw) === key) {
    return
  }

  publishedKeys.set(gw, key)
  gw.publishLocalEvent({
    payload: { session_key: key },
    ...(runtimeSessionId ? { session_id: runtimeSessionId } : {}),
    type: 'dashboard.active_session_changed'
  })
}
