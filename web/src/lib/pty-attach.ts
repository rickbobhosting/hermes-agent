export const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";

interface AttachTokenDependencies {
  storage?: Pick<Storage, "getItem" | "setItem">;
  fillRandom?: (bytes: Uint8Array<ArrayBuffer>) => void;
}

/** Return the stable opaque identity for this browser's dashboard PTY. */
export function ptyAttachToken(
  rotate = false,
  dependencies: AttachTokenDependencies = {},
): string {
  const storage = dependencies.storage ?? window.localStorage;
  const fillRandom =
    dependencies.fillRandom ??
    ((bytes: Uint8Array<ArrayBuffer>) => crypto.getRandomValues(bytes));

  let token = "";
  if (!rotate) {
    try {
      token = storage.getItem(PTY_ATTACH_TOKEN_KEY) ?? "";
    } catch {
      /* private mode / disabled storage */
    }
  }
  if (!token) {
    const bytes = new Uint8Array(new ArrayBuffer(16));
    fillRandom(bytes);
    token = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    try {
      storage.setItem(PTY_ATTACH_TOKEN_KEY, token);
    } catch {
      /* private mode / disabled storage */
    }
  }
  return token;
}
