import { describe, expect, it, vi } from "vitest";

import {
  PTY_ATTACH_TOKEN_KEY,
  ptyActiveSessionKeyFromEvent,
  ptyAttachIdentity,
  ptyBindSessionIdentity,
  ptyRecoverSessionLineage,
  ptyAttachScope,
  ptyAttachStorageKey,
  ptySidecarChannel,
  ptyShouldReconnect,
  ptySessionKeyFromActivePayload,
  ptyTerminalRejection,
  resolvedPtyResume,
} from "./pty-attach";

function fakeStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
  };
}

function bytes(value: number) {
  return (target: Uint8Array<ArrayBuffer>) => target.fill(value);
}

describe("ptyAttachIdentity", () => {
  it("reuses the persisted token after a browser reconnect", () => {
    const target = { profile: "Default", resume: "session-1" };
    const key = ptyAttachStorageKey(ptyAttachScope(target));
    const existing = "12".repeat(16);
    const storage = fakeStorage({ [key]: existing });

    expect(
      ptyAttachIdentity(target, false, {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("should not generate");
        },
      }).attachToken,
    ).toBe(existing);
  });

  it("keeps profiles and resume targets in separate identities", () => {
    const storage = fakeStorage();
    let seed = 1;
    const fillRandom = (target: Uint8Array<ArrayBuffer>) => target.fill(seed++);

    const defaultFresh = ptyAttachIdentity(
      { profile: "default" },
      false,
      { getStorage: () => storage, fillRandom },
    );
    const workFresh = ptyAttachIdentity(
      { profile: "work" },
      false,
      { getStorage: () => storage, fillRandom },
    );
    const defaultResume = ptyAttachIdentity(
      { profile: "default", resume: "abc" },
      false,
      { getStorage: () => storage, fillRandom },
    );

    expect(
      new Set([
        defaultFresh.attachToken,
        workFresh.attachToken,
        defaultResume.attachToken,
      ]).size,
    ).toBe(3);
  });

  it("rotates only the requested scope", () => {
    const storage = fakeStorage();
    const first = ptyAttachIdentity(
      { profile: "alpha" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x11) },
    );
    const other = ptyAttachIdentity(
      { profile: "beta" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x22) },
    );
    const rotated = ptyAttachIdentity(
      { profile: "alpha" },
      true,
      { getStorage: () => storage, fillRandom: bytes(0x33) },
    );

    expect(rotated.attachToken).not.toBe(first.attachToken);
    expect(
      ptyAttachIdentity(
        { profile: "beta" },
        false,
        {
          getStorage: () => storage,
          fillRandom: () => {
            throw new Error("should reuse");
          },
        },
      ).attachToken,
    ).toBe(other.attachToken);
  });

  it("rotates a rejected resume target without leaving the saved chat", () => {
    const storage = fakeStorage();
    const profile = "resume-retry";
    const resume = "20260906_211000_a1b2c3";
    const first = ptyAttachIdentity(
      { profile, resume },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x31) },
    );
    const unrelated = ptyAttachIdentity(
      { profile, resume: "20260906_211001_d4e5f6" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x32) },
    );

    const retried = ptyAttachIdentity(
      { profile, resume },
      true,
      { getStorage: () => storage, fillRandom: bytes(0x33) },
    );

    expect(retried.scope).toBe(first.scope);
    expect(retried.attachToken).not.toBe(first.attachToken);
    expect(
      ptyAttachIdentity(
        { profile, resume: "20260906_211001_d4e5f6" },
        false,
        { getStorage: () => storage, fillRandom: bytes(0x34) },
      ).attachToken,
    ).toBe(unrelated.attachToken);
  });

  it("keeps this tab's identity when another tab rotates persisted storage", () => {
    const target = { profile: "shared-profile", resume: null };
    const storage = fakeStorage();
    const currentTab = ptyAttachIdentity(target, false, {
      getStorage: () => storage,
      fillRandom: bytes(0xaa),
    });
    const key = ptyAttachStorageKey(currentTab.scope);

    // Simulate Start fresh in another tab, which shares localStorage but has
    // independent module memory.
    storage.values.set(key, "bb".repeat(16));

    const reconnect = ptyAttachIdentity(target, false, {
      getStorage: () => storage,
      fillRandom: () => {
        throw new Error("a reconnect must retain module memory");
      },
    });
    expect(reconnect).toEqual(currentTab);
    expect(storage.values.get(key)).toBe("bb".repeat(16));
  });

  it("falls back to module memory when localStorage property access throws", () => {
    const target = { profile: "getter-throws", resume: "one" };
    const windowLike = Object.defineProperty({}, "localStorage", {
      get: () => {
        throw new Error("SecurityError");
      },
    }) as { localStorage: Storage };
    const getStorage = () => windowLike.localStorage;
    const first = ptyAttachIdentity(target, false, {
      getStorage,
      fillRandom: bytes(0x44),
    });
    const second = ptyAttachIdentity(target, false, {
      getStorage,
      fillRandom: () => {
        throw new Error("should reuse memory");
      },
    });

    expect(second).toEqual(first);
  });

  it("falls back to module memory when storage reads and writes throw", () => {
    const target = { profile: "methods-throw", resume: "two" };
    const storage = {
      getItem: () => {
        throw new Error("read blocked");
      },
      setItem: () => {
        throw new Error("write blocked");
      },
    };
    const first = ptyAttachIdentity(target, false, {
      getStorage: () => storage,
      fillRandom: bytes(0x55),
    });
    const second = ptyAttachIdentity(target, false, {
      getStorage: () => storage,
      fillRandom: () => {
        throw new Error("should reuse memory");
      },
    });

    expect(second.attachToken).toBe(first.attachToken);
  });

  it("derives one stable lower-case channel from the exact 128-bit token", () => {
    const token = "AB".repeat(16);
    expect(ptySidecarChannel(token)).toBe(`pty-${token.toLowerCase()}`);
    expect(ptySidecarChannel(token)).toBe(ptySidecarChannel(token));
  });

  it("keeps one identity while a parent resume URL rewrites to its descendant", () => {
    const resolution = {
      profile: "default",
      source: "parent",
      target: "latest-child",
      path: ["parent", "latest-child"],
    };
    const beforeRewrite = resolvedPtyResume("parent", "default", resolution);
    const afterRewrite = resolvedPtyResume(
      "latest-child",
      "default",
      resolution,
    );

    expect(beforeRewrite).toBe("latest-child");
    expect(afterRewrite).toBe(beforeRewrite);
    expect(ptyAttachScope({ profile: "default", resume: beforeRewrite })).toBe(
      ptyAttachScope({ profile: "default", resume: afterRewrite }),
    );
  });

  it("surfaces invalid, full, and superseded sessions without reconnecting", () => {
    expect(ptyTerminalRejection(4422)).toContain("no longer valid");
    expect(ptyTerminalRejection(4429)).toContain("maximum number");
    expect(ptyTerminalRejection(4409)).toContain("another browser tab");
    expect(ptyTerminalRejection(1006)).toBeNull();
  });

  it("reconnects after transient sender pressure without treating the PTY as ended", () => {
    expect(ptyShouldReconnect(1013, true)).toBe(true);
    expect(ptyShouldReconnect(1006, false)).toBe(true);
    expect(ptyShouldReconnect(4410, true)).toBe(false);
    expect(ptyShouldReconnect(4409, true)).toBe(false);
  });

  it("migrates one token through successive resolved resume descendants", () => {
    const storage = fakeStorage();
    const profile = "lineage-migration";
    const first = "lineage-d";
    const second = "lineage-d2";
    const third = "lineage-d3";
    const token = "8a".repeat(16);
    const firstKey = ptyAttachStorageKey(
      ptyAttachScope({ profile, resume: first }),
    );
    const staleSecondKey = ptyAttachStorageKey(
      ptyAttachScope({ profile, resume: second }),
    );
    storage.values.set(firstKey, token);
    storage.values.set(staleSecondKey, "7b".repeat(16));

    const atSecond = ptyAttachIdentity(
      { profile, resume: second, resumeAliases: [first, second] },
      false,
      {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("should migrate the source token");
        },
      },
    );
    const secondKey = ptyAttachStorageKey(atSecond.scope);

    expect(atSecond.attachToken).toBe(token);
    expect(storage.values.get(secondKey)).toBe(token);

    const atThird = ptyAttachIdentity(
      { profile, resume: third, resumeAliases: [second, third] },
      false,
      {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("should migrate the prior descendant token");
        },
      },
    );
    expect(atThird.attachToken).toBe(token);

    const unrelated = ptyAttachIdentity(
      { profile, resume: "unrelated-lineage" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x91) },
    );
    expect(unrelated.attachToken).not.toBe(token);
  });

  it("binds a fresh chat's persistent session to the same PTY identity", () => {
    const storage = fakeStorage();
    const profile = "fresh-binding";
    const sessionKey = "20260906_210000_abc123";
    const fresh = ptyAttachIdentity(
      { profile },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xa1) },
    );

    expect(
      ptyBindSessionIdentity(
        fresh,
        { profile, sessionKey },
        { getStorage: () => storage },
      ),
    ).toBe(true);

    const selected = ptyAttachIdentity(
      { profile, resume: sessionKey },
      false,
      {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("selection must reattach the fresh PTY");
        },
      },
    );
    expect(selected.attachToken).toBe(fresh.attachToken);
    expect(selected.channel).toBe(fresh.channel);
  });

  it("replaces an old session alias after /new while keeping profiles isolated", () => {
    const storage = fakeStorage();
    const profile = "replacement-profile";
    const firstSession = "20260906_210001_abc123";
    const secondSession = "20260906_210002_def456";
    const fresh = ptyAttachIdentity(
      { profile },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xa2) },
    );
    expect(
      ptyBindSessionIdentity(
        fresh,
        { profile, sessionKey: firstSession },
        { getStorage: () => storage },
      ),
    ).toBe(true);
    expect(
      ptyBindSessionIdentity(
        fresh,
        { profile, sessionKey: secondSession },
        { getStorage: () => storage },
      ),
    ).toBe(true);

    const oldSelection = ptyAttachIdentity(
      { profile, resume: firstSession },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xb2) },
    );
    const currentSelection = ptyAttachIdentity(
      { profile, resume: secondSession },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xc2) },
    );
    expect(oldSelection.attachToken).not.toBe(fresh.attachToken);
    expect(currentSelection.attachToken).toBe(fresh.attachToken);

    expect(
      ptyBindSessionIdentity(
        fresh,
        { profile: "different-profile", sessionKey: secondSession },
        { getStorage: () => storage },
      ),
    ).toBe(false);
    const otherProfile = ptyAttachIdentity(
      { profile: "different-profile", resume: secondSession },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xd2) },
    );
    expect(otherProfile.attachToken).not.toBe(fresh.attachToken);
  });

  it("accepts only a validated persistent key from an active-session payload", () => {
    const sessionKey = "20260906_210003_ab12ef";
    expect(ptySessionKeyFromActivePayload({ session_key: sessionKey })).toBe(
      sessionKey,
    );
    expect(
      ptySessionKeyFromActivePayload({
        session_key: "forged\nkey",
      }),
    ).toBeNull();
    expect(
      ptySessionKeyFromActivePayload(Object.create({ session_key: sessionKey })),
    ).toBeNull();
    expect(ptySessionKeyFromActivePayload(sessionKey)).toBeNull();
    expect(
      ptyActiveSessionKeyFromEvent("dashboard.active_session_changed", {
        session_key: sessionKey,
      }),
    ).toBe(sessionKey);
    expect(
      ptyActiveSessionKeyFromEvent("session.info", {
        session_key: sessionKey,
      }),
    ).toBeNull();
  });

  it("restores old aliases only for a confirmed descendant lineage", () => {
    const storage = fakeStorage();
    const profile = "confirmed-lineage";
    const parent = "20260906_212000_aabbcc";
    const child = "20260906_212001_ddeeff";
    const fresh = ptyAttachIdentity(
      { profile },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xe1) },
    );

    ptyBindSessionIdentity(
      fresh,
      { profile, sessionKey: parent },
      { getStorage: () => storage },
    );
    ptyBindSessionIdentity(
      fresh,
      { profile, sessionKey: child },
      { getStorage: () => storage },
    );
    ptyBindSessionIdentity(
      fresh,
      { profile, sessionKey: child, lineage: [parent, child] },
      { getStorage: () => storage },
    );

    expect(
      ptyAttachIdentity(
        { profile, resume: parent },
        false,
        { getStorage: () => storage, fillRandom: bytes(0xe2) },
      ).attachToken,
    ).toBe(fresh.attachToken);
  });

  it("reuses a bound persistent session after a browser module reload", async () => {
    const storage = fakeStorage();
    const profile = "binding-reload";
    const sessionKey = "20260906_212100_1122aa";
    const fresh = ptyAttachIdentity(
      { profile },
      false,
      { getStorage: () => storage, fillRandom: bytes(0xe3) },
    );
    ptyBindSessionIdentity(
      fresh,
      { profile, sessionKey },
      { getStorage: () => storage },
    );

    vi.resetModules();
    const reloadedModule = await import("./pty-attach");
    const selected = reloadedModule.ptyAttachIdentity(
      { profile, resume: sessionKey },
      false,
      {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("reload should reuse the persisted binding");
        },
      },
    );
    expect(selected.attachToken).toBe(fresh.attachToken);
  });

  it("recovers every confirmed lineage alias with one token across reload", async () => {
    const storage = fakeStorage();
    const profile = "lineage-tombstone-recovery";
    const parent = "20260906_213000_aa11bb";
    const child = "20260906_213001_cc22dd";
    const unrelatedKey = "20260906_213002_ee33ff";
    const rejected = ptyAttachIdentity(
      { profile, resume: child },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x41) },
    );
    ptyBindSessionIdentity(
      rejected,
      { profile, sessionKey: child, lineage: [parent, child] },
      { getStorage: () => storage },
    );
    const unrelated = ptyAttachIdentity(
      { profile, resume: unrelatedKey },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x42) },
    );

    const recovered = ptyRecoverSessionLineage(
      rejected,
      { profile, sessionKey: child, lineage: [parent, child] },
      { getStorage: () => storage, fillRandom: bytes(0x43) },
    );
    expect(recovered).not.toBeNull();
    expect(recovered?.attachToken).not.toBe(rejected.attachToken);

    vi.resetModules();
    const reloadedModule = await import("./pty-attach");
    const reopenedFromParent = reloadedModule.ptyAttachIdentity(
      { profile, resume: child, resumeAliases: [parent, child] },
      false,
      {
        getStorage: () => storage,
        fillRandom: () => {
          throw new Error("confirmed parent must resolve to recovered token");
        },
      },
    );
    expect(reopenedFromParent.attachToken).toBe(recovered?.attachToken);
    expect(
      reloadedModule.ptyAttachIdentity(
        { profile, resume: unrelatedKey },
        false,
        { getStorage: () => storage, fillRandom: bytes(0x44) },
      ).attachToken,
    ).toBe(unrelated.attachToken);
  });

  it("does not let normal active events overwrite another tab's rotation", () => {
    const storage = fakeStorage();
    const profile = "binding-cross-tab";
    const sessionKey = "20260906_213100_abcdef";
    const current = ptyAttachIdentity(
      { profile },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x45) },
    );
    ptyBindSessionIdentity(
      current,
      { profile, sessionKey },
      { getStorage: () => storage },
    );
    const sessionStorageKey = ptyAttachStorageKey(
      ptyAttachScope({ profile, resume: sessionKey }),
    );
    const otherTabToken = "46".repeat(16);
    storage.values.set(sessionStorageKey, otherTabToken);

    ptyBindSessionIdentity(
      current,
      { profile, sessionKey },
      { getStorage: () => storage },
    );
    expect(storage.values.get(sessionStorageKey)).toBe(otherTabToken);
  });

  it("migrates the legacy global token only into a fresh default new-chat scope", async () => {
    // A browser reload gets a new module realm with no tab-pinned identity.
    vi.resetModules();
    const freshModule = await import("./pty-attach");
    const legacy = "cd".repeat(16);
    const storage = fakeStorage({ [PTY_ATTACH_TOKEN_KEY]: legacy });
    const migrated = freshModule.ptyAttachIdentity(
      { profile: "default" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x66) },
    );
    const resumed = freshModule.ptyAttachIdentity(
      { profile: "default", resume: "different" },
      false,
      { getStorage: () => storage, fillRandom: bytes(0x77) },
    );

    expect(migrated.attachToken).toBe(legacy);
    expect(resumed.attachToken).not.toBe(legacy);
  });
});
