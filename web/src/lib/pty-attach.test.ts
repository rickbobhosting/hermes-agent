import { describe, expect, it } from "vitest";

import { PTY_ATTACH_TOKEN_KEY, ptyAttachToken } from "./pty-attach";

function fakeStorage(initial = "") {
  const values = new Map<string, string>();
  if (initial) values.set(PTY_ATTACH_TOKEN_KEY, initial);
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
  };
}

describe("ptyAttachToken", () => {
  it("reuses the persisted token after a browser reconnect", () => {
    const storage = fakeStorage("existing-token");
    expect(
      ptyAttachToken(false, {
        storage,
        fillRandom: () => {
          throw new Error("should not generate");
        },
      }),
    ).toBe("existing-token");
  });

  it("rotates and persists a fresh 128-bit token for a new chat", () => {
    const storage = fakeStorage("old-token");
    const token = ptyAttachToken(true, {
      storage,
      fillRandom: (bytes) => bytes.fill(0xab),
    });
    expect(token).toBe("ab".repeat(16));
    expect(storage.values.get(PTY_ATTACH_TOKEN_KEY)).toBe(token);
  });
});
