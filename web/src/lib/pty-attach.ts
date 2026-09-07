export const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";
export const PTY_ATTACH_TOKEN_KEY_PREFIX = `${PTY_ATTACH_TOKEN_KEY}.v2.`;

type AttachStorage = Pick<Storage, "getItem" | "setItem"> &
  Partial<Pick<Storage, "removeItem">>;

export interface AttachTokenDependencies {
  /** Test seam; the callback also lets us exercise a throwing localStorage getter. */
  getStorage?: () => AttachStorage | undefined;
  fillRandom?: (bytes: Uint8Array<ArrayBuffer>) => void;
}

export interface PtyAttachTarget {
  /** Canonical selected profile. Empty means the dashboard process profile. */
  profile?: string | null;
  /** Canonical session id to resume. Null/empty means a new-chat target. */
  resume?: string | null;
  /** Earlier ids in the same resolved resume lineage, oldest first. */
  resumeAliases?: readonly string[];
}

export interface PtyAttachIdentity {
  attachToken: string;
  channel: string;
  profile: string;
  scope: string;
}

export interface PtyResumeResolution {
  confirmed?: boolean;
  profile: string;
  source: string;
  target: string;
  path: string[];
}

// localStorage can be absent or throw at three distinct points: accessing the
// property, reading a key, or writing a key. Keep the last identity for each
// logical target in module memory so all three failure modes still reconnect
// consistently for the lifetime of this page.
const memoryTokens = new Map<string, string>();
const memorySessionBindings = new Map<string, string[]>();
const TOKEN_RE = /^[0-9a-f]{32}$/i;
const PTY_REGISTRY_KEY_DELIMITER = "\x1f";

function defaultStorage(): AttachStorage | undefined {
  if (typeof window === "undefined") return undefined;
  return window.localStorage;
}

function canonicalProfile(profile?: string | null): string {
  return profile?.trim().toLowerCase() || "default";
}

/** Stable serialization for a logical dashboard-chat target. */
export function ptyAttachScope({ profile, resume }: PtyAttachTarget): string {
  return JSON.stringify([
    canonicalProfile(profile),
    resume?.trim() ? `resume:${resume.trim()}` : "new-chat",
  ]);
}

/**
 * Return the canonical resume target once lineage lookup settles.
 *
 * Both the originally requested parent and its rewritten descendant map to the
 * same target, keeping the attach token/channel stable through URL cleanup.
 * `undefined` means the caller must wait before opening a PTY.
 */
export function resolvedPtyResume(
  requested: string | null,
  profile: string,
  resolution: PtyResumeResolution | null,
): string | null | undefined {
  if (!requested) return null;
  if (
    resolution?.profile === profile &&
    (resolution.source === requested ||
      resolution.target === requested ||
      resolution.path.includes(requested))
  ) {
    return resolution.target;
  }
  return undefined;
}

export function ptyAttachStorageKey(scope: string): string {
  return `${PTY_ATTACH_TOKEN_KEY_PREFIX}${encodeURIComponent(scope)}`;
}

export function ptySidecarChannel(attachToken: string): string {
  return `pty-${attachToken.toLowerCase()}`;
}

/** Match the backend's accepted persistent resume-key envelope. */
export function validatedPtySessionKey(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const sessionKey = value.trim();
  if (
    !sessionKey ||
    sessionKey.length > 512 ||
    sessionKey.includes(PTY_REGISTRY_KEY_DELIMITER) ||
    Array.from(sessionKey).some(
      (character) => character.charCodeAt(0) < 0x20 || character === "\x7f",
    )
  ) {
    return null;
  }
  return sessionKey;
}

/** Read the persistent key carried by a dedicated active-session payload. */
export function ptySessionKeyFromActivePayload(payload: unknown): string | null {
  if (
    !payload ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    !Object.prototype.hasOwnProperty.call(payload, "session_key")
  ) {
    return null;
  }
  return validatedPtySessionKey(
    (payload as Record<string, unknown>).session_key,
  );
}

/** Generic gateway events are not authoritative for the foreground PTY. */
export function ptyActiveSessionKeyFromEvent(
  type: unknown,
  payload: unknown,
): string | null {
  return type === "dashboard.active_session_changed"
    ? ptySessionKeyFromActivePayload(payload)
    : null;
}

/** User-facing terminal rejections that must never enter the reconnect loop. */
export function ptyTerminalRejection(code: number): string | null {
  if (code === 4422) {
    return "This saved chat connection is no longer valid. Open it in a new task to continue.";
  }
  if (code === 4429) {
    return "Hermes is already running the maximum number of background tasks. Stop one below, then try again.";
  }
  if (code === 4409) {
    return "This chat is attached in another browser tab. Close it there or choose Start fresh here.";
  }
  return null;
}

/** Close codes for which the server-owned PTY may still be available. */
export function ptyShouldReconnect(code: number, wasClean: boolean): boolean {
  return !wasClean || code === 1001 || code === 1006 || code === 1013;
}

function ptySessionBindingStorageKey(
  profile: string,
  attachToken: string,
): string {
  return `${PTY_ATTACH_TOKEN_KEY_PREFIX}binding.${encodeURIComponent(
    JSON.stringify([profile, attachToken]),
  )}`;
}

/**
 * Bind a PTY's current persistent session key to its existing attach token.
 *
 * A `/new` replaces the prior aliases. Callers may later supply a confirmed
 * descendant lineage to restore true ancestor aliases after canonicalization.
 */
export function ptyBindSessionIdentity(
  identity: PtyAttachIdentity,
  target: {
    profile?: string | null;
    sessionKey: unknown;
    lineage?: readonly unknown[];
    /** Explicit recovery may replace aliases still owned by this old token. */
    replacePersistedToken?: string;
  },
  dependencies: AttachTokenDependencies = {},
): boolean {
  const profile = canonicalProfile(target.profile);
  const sessionKey = validatedPtySessionKey(target.sessionKey);
  if (
    !sessionKey ||
    profile !== identity.profile ||
    !TOKEN_RE.test(identity.attachToken)
  ) {
    return false;
  }

  const lineage = target.lineage ?? [];
  const aliases: string[] = [];
  for (const candidate of [...lineage, sessionKey]) {
    const alias = validatedPtySessionKey(candidate);
    if (!alias) return false;
    if (!aliases.includes(alias)) aliases.push(alias);
  }

  let storage: AttachStorage | undefined;
  try {
    storage = (dependencies.getStorage ?? defaultStorage)();
  } catch {
    // Module memory still keeps bindings coherent for this page lifetime.
  }

  const attachToken = identity.attachToken.toLowerCase();
  const replacePersistedToken = TOKEN_RE.test(
    target.replacePersistedToken ?? "",
  )
    ? target.replacePersistedToken!.toLowerCase()
    : null;
  const bindingKey = ptySessionBindingStorageKey(profile, attachToken);
  let previous = memorySessionBindings.get(bindingKey);
  if (!previous && storage) {
    try {
      const parsed: unknown = JSON.parse(storage.getItem(bindingKey) ?? "null");
      if (Array.isArray(parsed)) {
        const valid = parsed.map(validatedPtySessionKey);
        if (valid.every((alias): alias is string => alias !== null)) {
          previous = Array.from(new Set(valid));
        }
      }
    } catch {
      // A blocked/corrupt binding record cannot override trusted module state.
    }
  }

  for (const prior of previous ?? []) {
    if (aliases.includes(prior)) continue;
    const priorKey = ptyAttachStorageKey(
      ptyAttachScope({ profile, resume: prior }),
    );
    if (memoryTokens.get(priorKey) === attachToken) {
      memoryTokens.delete(priorKey);
    }
    if (storage?.removeItem) {
      try {
        if ((storage.getItem(priorKey) ?? "").toLowerCase() === attachToken) {
          storage.removeItem(priorKey);
        }
      } catch {
        // Never remove an alias if storage cannot prove this token owns it.
      }
    }
  }

  for (const alias of aliases) {
    const aliasKey = ptyAttachStorageKey(
      ptyAttachScope({ profile, resume: alias }),
    );
    const memoryToken = memoryTokens.get(aliasKey)?.toLowerCase() ?? "";
    if (
      !replacePersistedToken ||
      !memoryToken ||
      memoryToken === attachToken ||
      memoryToken === replacePersistedToken
    ) {
      memoryTokens.set(aliasKey, attachToken);
    }
    if (storage) {
      try {
        const persisted = storage.getItem(aliasKey) ?? "";
        // A duplicate event in this tab must not undo another tab's explicit
        // rotation of the same session scope. Current-tab module memory stays
        // pinned, while a fresh browser adopts the latest persisted owner.
        if (
          !persisted ||
          persisted.toLowerCase() === attachToken ||
          persisted.toLowerCase() === replacePersistedToken
        ) {
          storage.setItem(aliasKey, attachToken);
        }
      } catch {
        // Module memory still supports selection during this page lifetime.
      }
    }
  }

  memorySessionBindings.set(bindingKey, aliases);
  if (storage) {
    try {
      storage.setItem(bindingKey, JSON.stringify(aliases));
    } catch {
      // Best-effort persistence; current-tab replacement is still correct.
    }
  }
  return true;
}

/** Rotate one rejected resume plus only its already-confirmed lineage aliases. */
export function ptyRecoverSessionLineage(
  rejectedIdentity: PtyAttachIdentity,
  target: {
    profile?: string | null;
    sessionKey: unknown;
    lineage?: readonly unknown[];
  },
  dependencies: AttachTokenDependencies = {},
): PtyAttachIdentity | null {
  const profile = canonicalProfile(target.profile);
  const sessionKey = validatedPtySessionKey(target.sessionKey);
  const lineage = target.lineage ?? [];
  if (
    !sessionKey ||
    profile !== rejectedIdentity.profile ||
    !TOKEN_RE.test(rejectedIdentity.attachToken) ||
    lineage.some((alias) => !validatedPtySessionKey(alias))
  ) {
    return null;
  }

  const recovered = ptyAttachIdentity(
    { profile, resume: sessionKey },
    true,
    dependencies,
  );
  ptyBindSessionIdentity(
    recovered,
    {
      profile,
      sessionKey,
      lineage,
      replacePersistedToken: rejectedIdentity.attachToken,
    },
    dependencies,
  );
  return recovered;
}

function freshToken(fillRandom: (bytes: Uint8Array<ArrayBuffer>) => void): string {
  const bytes = new Uint8Array(new ArrayBuffer(16));
  fillRandom(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

/**
 * Return the stable opaque PTY identity for one logical chat target.
 *
 * Normal reconnects and browser reopens reuse the target's token. An explicit
 * Start fresh rotates only the supplied target. The sidecar channel is always
 * derived from that exact token, so terminal and event subscriber cannot drift.
 */
export function ptyAttachIdentity(
  target: PtyAttachTarget,
  rotate = false,
  dependencies: AttachTokenDependencies = {},
): PtyAttachIdentity {
  const scope = ptyAttachScope(target);
  const storageKey = ptyAttachStorageKey(scope);
  const aliasStorageKeys = Array.from(
    new Set(
      (target.resumeAliases ?? [])
        .map((resume) => resume.trim())
        .filter((resume) => resume && resume !== target.resume?.trim())
        .map((resume) =>
          ptyAttachStorageKey(
            ptyAttachScope({ profile: target.profile, resume }),
          ),
        ),
    ),
  );
  const fillRandom =
    dependencies.fillRandom ??
    ((bytes: Uint8Array<ArrayBuffer>) => crypto.getRandomValues(bytes));

  let storage: AttachStorage | undefined;
  try {
    storage = (dependencies.getStorage ?? defaultStorage)();
  } catch {
    // Accessing window.localStorage itself may throw in hardened/private modes.
  }

  let token = rotate ? "" : memoryTokens.get(storageKey) ?? "";
  let persistToken = rotate;
  // A mounted tab owns its in-memory identity. Another tab can rotate the same
  // localStorage scope, but a routine reconnect here must not adopt that newer
  // token and steal the other tab's PTY. A fresh browser/module (no memory)
  // still discovers the latest persisted token normally.
  if (!rotate && !TOKEN_RE.test(token)) {
    for (const aliasKey of aliasStorageKeys) {
      const aliasToken = memoryTokens.get(aliasKey) ?? "";
      if (TOKEN_RE.test(aliasToken)) {
        token = aliasToken.toLowerCase();
        persistToken = true;
        break;
      }
    }
  }

  if (!rotate && !TOKEN_RE.test(token) && storage) {
    try {
      // On a fresh page load, continuity with the requested ancestor takes
      // precedence over a stale target token. Persisting the choice into the
      // canonical target lets each later descendant hop continue the chain.
      for (const aliasKey of aliasStorageKeys) {
        const persistedAlias = storage.getItem(aliasKey) ?? "";
        if (TOKEN_RE.test(persistedAlias)) {
          token = persistedAlias.toLowerCase();
          persistToken = true;
          break;
        }
      }

      const persisted = storage.getItem(storageKey) ?? "";
      if (!TOKEN_RE.test(token) && TOKEN_RE.test(persisted)) {
        token = persisted.toLowerCase();
      }

      // Preserve a running pre-v2 default/new-chat PTY across upgrade. Other
      // targets never read the legacy global key, which prevents identity theft.
      if (
        !TOKEN_RE.test(token) &&
        scope === ptyAttachScope({ profile: "default" })
      ) {
        const legacy = storage.getItem(PTY_ATTACH_TOKEN_KEY) ?? "";
        if (TOKEN_RE.test(legacy)) {
          token = legacy.toLowerCase();
          persistToken = true;
        }
      }
    } catch {
      // The in-memory value above remains authoritative when reads are blocked.
    }
  }

  if (!TOKEN_RE.test(token)) {
    token = freshToken(fillRandom);
    persistToken = true;
  }
  token = token.toLowerCase();
  memoryTokens.set(storageKey, token);

  if (storage && persistToken) {
    try {
      storage.setItem(storageKey, token);
    } catch {
      // Module memory still provides stable reconnects for this page lifetime.
    }
  }

  return {
    attachToken: token,
    channel: ptySidecarChannel(token),
    profile: canonicalProfile(target.profile),
    scope,
  };
}

/** Compatibility helper for callers that need only the default token. */
export function ptyAttachToken(
  rotate = false,
  dependencies: AttachTokenDependencies = {},
): string {
  return ptyAttachIdentity({}, rotate, dependencies).attachToken;
}
