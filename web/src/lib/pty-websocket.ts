/**
 * Complete the asynchronous half of a PTY WebSocket open without leaking a
 * rejected promise or a synchronous browser constructor exception.
 */
export async function openPtyWebSocket<T>(
  buildUrl: () => Promise<string>,
  isCurrent: () => boolean,
  createSocket: (url: string) => T,
): Promise<T | null> {
  let url: string;
  try {
    url = await buildUrl();
  } catch {
    return null;
  }

  if (!isCurrent()) return null;

  try {
    return createSocket(url);
  } catch {
    return null;
  }
}
