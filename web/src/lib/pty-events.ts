import { buildWsUrl } from "@/lib/api";

interface EventSocket {
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "error", listener: () => void): void;
  addEventListener(
    type: "close",
    listener: (event: Pick<CloseEvent, "code" | "reason">) => void,
  ): void;
  addEventListener(
    type: "message",
    listener: (event: Pick<MessageEvent, "data">) => void,
  ): void;
  close(): void;
}

interface EventEnvelope {
  method?: string;
  params?: { type?: string; payload?: unknown };
}

export interface PtyEventSubscriptionOptions {
  channel: string;
  onEvent: (type: string, payload: unknown) => void;
  onOpen?: () => void;
  onDisconnect?: (code: number) => void;
  onTerminalClose?: (code: number, reason: string) => void;
  buildUrl?: (channel: string) => Promise<string>;
  createSocket?: (url: string) => EventSocket;
}

const TERMINAL_CLOSE_CODES = new Set([4400, 4401, 4403, 4404, 4408]);

/** Subscribe to one PTY event channel until explicitly stopped. */
export function subscribeToPtyEvents({
  channel,
  onEvent,
  onOpen,
  onDisconnect,
  onTerminalClose,
  buildUrl = (value) => buildWsUrl("/api/events", { channel: value }),
  createSocket = (url) => new WebSocket(url),
}: PtyEventSubscriptionOptions): () => void {
  let stopped = false;
  let connecting = false;
  let socket: EventSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryAttempt = 0;
  let generation = 0;

  const scheduleReconnect = (code: number) => {
    if (stopped || connecting || socket || retryTimer) return;
    retryAttempt = Math.min(retryAttempt + 1, 5);
    const delay = Math.min(250 * 2 ** (retryAttempt - 1), 3000);
    onDisconnect?.(code);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void connect();
    }, delay);
  };

  const connect = async () => {
    if (stopped || connecting || socket) return;
    connecting = true;
    const ownGeneration = ++generation;

    let url: string;
    try {
      url = await buildUrl(channel);
    } catch {
      connecting = false;
      if (!stopped && ownGeneration === generation) scheduleReconnect(1006);
      return;
    }

    connecting = false;
    if (stopped || ownGeneration !== generation) return;

    let current: EventSocket;
    try {
      current = createSocket(url);
    } catch {
      scheduleReconnect(1006);
      return;
    }
    socket = current;
    current.addEventListener("open", () => {
      if (stopped || socket !== current) return;
      retryAttempt = 0;
      onOpen?.();
    });
    current.addEventListener("error", () => {
      if (stopped || socket !== current) return;
      // Browsers normally follow `error` with `close`, but that is not a safe
      // recovery contract. Retire this exact socket first so a synchronous or
      // delayed close cannot schedule a second retry.
      socket = null;
      try {
        current.close();
      } catch {
        // The retry below is authoritative even if close itself fails.
      }
      scheduleReconnect(1006);
    });
    current.addEventListener("close", (event) => {
      if (stopped || socket !== current) return;
      socket = null;
      if (TERMINAL_CLOSE_CODES.has(event.code)) {
        onTerminalClose?.(event.code, event.reason);
        return;
      }
      scheduleReconnect(event.code);
    });
    current.addEventListener("message", (event) => {
      if (stopped || socket !== current || typeof event.data !== "string") {
        return;
      }
      let frame: EventEnvelope;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      const type = frame.method === "event" ? frame.params?.type : undefined;
      if (type) onEvent(type, frame.params?.payload);
    });
  };

  void connect();

  return () => {
    stopped = true;
    generation += 1;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    const current = socket;
    socket = null;
    current?.close();
  };
}
