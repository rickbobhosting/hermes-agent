import assert from 'node:assert/strict';
import { chromium, expect } from '@playwright/test';

const [base, sessionId, chromePath] = process.argv.slice(2);
const browser = await chromium.launch({
  ...(chromePath ? { executablePath: chromePath } : {}),
  headless: true,
  args: ['--no-sandbox'],
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  // The sidebar's separate JSON-RPC connection is status-only here. Keep the
  // browser from constructing a real agent; /api/pty remains the real server.
  await page.routeWebSocket('**/api/ws*', socket => socket.close());
  const unauthorized = await page.request.get(`${base}/api/sessions?profile=acceptance`);
  assert.equal(unauthorized.status(), 401, 'protected session API must require the injected token');
  const ptyFrames = [];
  page.on('websocket', socket => {
    if (!socket.url().includes('/api/pty?')) return;
    socket.on('framereceived', frame => ptyFrames.push(String(frame.payload)));
  });

  await page.goto(`${base}/sessions`, { waitUntil: 'domcontentloaded' });
  const switcher = page.locator('#hermes-profile-switcher');
  await expect(switcher).toBeVisible();
  await switcher.locator('button').click();
  await switcher.getByRole('option', { name: 'acceptance' }).click();
  await expect(switcher.locator('button')).toContainText('acceptance');
  await page.getByRole('link', { name: 'Chat', exact: true }).click();

  const saved = page.getByText('Fixture continuity', { exact: true });
  await expect(saved).toBeVisible();
  await saved.click();
  await expect.poll(() => new URL(page.url()).searchParams.get('resume')).toBe(sessionId);
  const terminal = page.locator('.hermes-chat-xterm-host');
  await expect(terminal.getByRole('textbox', { name: 'Terminal input' })).toBeVisible();
  await expect.poll(() => ptyFrames.join('')).toContain('first saved answer');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture ready>');

  ptyFrames.length = 0;
  await terminal.click();
  await page.keyboard.type('browser turn one');
  await page.keyboard.press('Enter');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture reply 2: browser turn one');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture ready>');

  ptyFrames.length = 0;
  await terminal.click();
  await page.keyboard.type('browser turn two');
  await page.keyboard.press('Enter');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture reply 3: browser turn two');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture ready>');

  ptyFrames.length = 0;
  await terminal.click();
  await page.keyboard.type('slow fixture turn');
  await page.keyboard.press('Enter');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture pending');
  await page.keyboard.press('Control+C');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture cancelled');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture ready>');

  ptyFrames.length = 0;
  await terminal.click();
  await page.keyboard.type('browser after cancel');
  await page.keyboard.press('Enter');
  await expect.poll(() => ptyFrames.join('')).toContain('fixture reply 4: browser after cancel');

  const authorized = await page.evaluate(async id => {
    const token = window.__HERMES_SESSION_TOKEN__;
    const response = await fetch(`/api/sessions/${id}/messages?profile=acceptance`, {
      headers: { 'X-Hermes-Session-Token': token },
    });
    return { status: response.status, body: await response.json() };
  }, sessionId);
  assert.equal(authorized.status, 200);
  assert.deepEqual(authorized.body.messages.map(m => m.content), [
    'first saved question', 'first saved answer',
    'browser turn one', 'fixture reply 2: browser turn one',
    'browser turn two', 'fixture reply 3: browser turn two',
    'browser after cancel', 'fixture reply 4: browser after cancel',
  ]);

  ptyFrames.length = 0;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => new URL(page.url()).searchParams.get('profile')).toBe('acceptance');
  await expect.poll(() => new URL(page.url()).searchParams.get('resume')).toBe(sessionId);
  await expect.poll(() => ptyFrames.join('')).toContain('fixture reply 4: browser after cancel');
  await expect(page.getByRole('heading', { name: 'Fixture continuity' })).toBeVisible();
  console.log('authenticated dashboard continuity accepted');
} finally {
  await browser.close();
}
