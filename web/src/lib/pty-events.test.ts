import { afterEach, describe, expect, it, vi } from "vitest";

import { ptyActiveSessionKeyFromEvent } from "./pty-attach";
import { subscribeToPtyEvents } from "./pty-events";

type Listener = (event: never) => void;

class FakeSocket {
  closed = false;
  listeners = new Map<string, Listener[]>();
  emitCloseOnClose: boolean;

  constructor(emitCloseOnClose = true) {
    this.emitCloseOnClose = emitCloseOnClose;
  }

  addEventListener(type: string, listener: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  emit(type: string, event: unknown = {}) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event as never);
    }
  }

  close() {
    this.closed = true;
    if (this.emitCloseOnClose) {
      this.emit("close", { code: 1000, reason: "client cleanup" });
    }
  }
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("subscribeToPtyEvents", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("reconnects after a transient close and consumes the replayed active key", async () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const keys: string[] = [];
    const buildUrl = vi.fn(async () => "ws://dashboard/api/events");
    const stop = subscribeToPtyEvents({
      channel: "pty-test",
      buildUrl,
      createSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      onEvent: (type, payload) => {
        const key = ptyActiveSessionKeyFromEvent(type, payload);
        if (key) keys.push(key);
      },
    });
    await settle();
    expect(sockets).toHaveLength(1);

    sockets[0].emit("close", { code: 1006, reason: "network" });
    await vi.advanceTimersByTimeAsync(249);
    expect(sockets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(sockets).toHaveLength(2);

    sockets[1].emit("open");
    sockets[1].emit("message", {
      data: JSON.stringify({
        method: "event",
        params: {
          type: "dashboard.active_session_changed",
          payload: { session_key: "20260906_220000_a1b2c3" },
        },
      }),
    });
    expect(keys).toEqual(["20260906_220000_a1b2c3"]);

    // A successful open resets backoff to the initial 250 ms.
    sockets[1].emit("close", { code: 1013, reason: "try again" });
    await vi.advanceTimersByTimeAsync(250);
    await settle();
    expect(sockets).toHaveLength(3);
    stop();
  });

  it("does not reconnect terminal closes or after cleanup", async () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const terminal = vi.fn();
    const options = {
      channel: "pty-test",
      buildUrl: async () => "ws://dashboard/api/events",
      createSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      onEvent: vi.fn(),
      onTerminalClose: terminal,
    };

    subscribeToPtyEvents(options);
    await settle();
    sockets[0].emit("close", { code: 4401, reason: "auth" });
    await vi.runAllTimersAsync();
    expect(sockets).toHaveLength(1);
    expect(terminal).toHaveBeenCalledWith(4401, "auth");

    const stop = subscribeToPtyEvents(options);
    await settle();
    sockets[1].emit("close", { code: 1006, reason: "network" });
    stop();
    await vi.runAllTimersAsync();
    expect(sockets).toHaveLength(2);
  });

  it("retries when WebSocket construction throws", async () => {
    vi.useFakeTimers();
    let attempts = 0;
    const sockets: FakeSocket[] = [];
    const stop = subscribeToPtyEvents({
      channel: "pty-test",
      buildUrl: async () => "ws://dashboard/api/events",
      createSocket: () => {
        attempts += 1;
        if (attempts === 1) throw new Error("constructor failed");
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      onEvent: vi.fn(),
    });
    await settle();
    expect(attempts).toBe(1);

    await vi.advanceTimersByTimeAsync(250);
    await settle();
    expect(attempts).toBe(2);
    expect(sockets).toHaveLength(1);
    stop();
  });

  it("retires an errored socket and retries even when close never follows", async () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const stop = subscribeToPtyEvents({
      channel: "pty-test",
      buildUrl: async () => "ws://dashboard/api/events",
      createSocket: () => {
        const socket = new FakeSocket(false);
        sockets.push(socket);
        return socket;
      },
      onEvent: vi.fn(),
    });
    await settle();

    const failed = sockets[0];
    failed.emit("error");
    expect(failed.closed).toBe(true);
    await vi.advanceTimersByTimeAsync(250);
    await settle();
    expect(sockets).toHaveLength(2);

    // A late close for the retired socket cannot enqueue another attempt.
    failed.emit("close", { code: 1006, reason: "late close" });
    expect(vi.getTimerCount()).toBe(0);
    stop();
  });
});
