import { describe, expect, it, vi } from "vitest";

import { openPtyWebSocket } from "./pty-websocket";

describe("openPtyWebSocket", () => {
  it("contains an initial ticket failure and a retry constructor failure", async () => {
    const createSocket = vi.fn(() => {
      throw new Error("browser constructor rejected URL");
    });

    await expect(
      openPtyWebSocket(
        async () => {
          throw new Error("ticket unavailable");
        },
        () => true,
        createSocket,
      ),
    ).resolves.toBeNull();
    expect(createSocket).not.toHaveBeenCalled();

    await expect(
      openPtyWebSocket(
        async () => "ws://dashboard/api/pty",
        () => true,
        createSocket,
      ),
    ).resolves.toBeNull();
    expect(createSocket).toHaveBeenCalledOnce();
  });

  it("does not construct a stale socket after URL building completes", async () => {
    const createSocket = vi.fn(() => ({ close: vi.fn() }));
    await expect(
      openPtyWebSocket(
        async () => "ws://dashboard/api/pty",
        () => false,
        createSocket,
      ),
    ).resolves.toBeNull();
    expect(createSocket).not.toHaveBeenCalled();
  });
});
