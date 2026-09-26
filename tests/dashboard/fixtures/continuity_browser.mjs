import assert from 'node:assert/strict';
import { chromium, expect } from '@playwright/test';

const [base, sessionId, chromePath, mode] = process.argv.slice(2);
const fleet = mode === 'fleet';
const profile = fleet ? 'fleet:infra' : 'acceptance';
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
  const unauthorized = await page.request.get(fleet
    ? `${base}/api/profiles/fleet/infra/sessions`
    : `${base}/api/sessions?profile=acceptance`);
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
  await expect.poll(async () => (await switcher.getByRole('option').allTextContents()).join('|'))
    .toMatch(fleet ? /Infra.*chat/ : /acceptance/);
  await switcher.getByRole('option', { name: fleet ? /Infra.*chat/ : 'acceptance' }).click();
  await expect(switcher.locator('button')).toContainText(fleet ? 'Infra' : 'acceptance');
  if (fleet) {
    await page.getByRole('button', { name: 'Resume Fixture continuity' }).click();
    await expect(page.getByRole('link', { name: 'Chat', exact: true })).toBeVisible();
    await expect(page.locator('[role="complementary"]')).toHaveCount(0);
  } else {
    await page.getByRole('link', { name: 'Chat', exact: true }).click();
    const saved = page.getByText('Fixture continuity', { exact: true });
    await expect(saved).toBeVisible();
    await saved.click();
  }
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

  const authorized = await page.evaluate(async ({ id, fleet }) => {
    const token = window.__HERMES_SESSION_TOKEN__;
    const path = fleet
      ? `/api/profiles/fleet/infra/sessions/${id}/messages?limit=500&order=oldest`
      : `/api/sessions/${id}/messages?profile=acceptance`;
    const response = await fetch(path, {
      headers: { 'X-Hermes-Session-Token': token },
    });
    return { status: response.status, body: await response.json() };
  }, { id: sessionId, fleet });
  assert.equal(authorized.status, 200);
  assert.deepEqual(authorized.body.messages.map(m => m.content), [
    'first saved question', 'first saved answer',
    'browser turn one', 'fixture reply 2: browser turn one',
    'browser turn two', 'fixture reply 3: browser turn two',
    'browser after cancel', 'fixture reply 4: browser after cancel',
  ]);

  ptyFrames.length = 0;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => new URL(page.url()).searchParams.get('profile')).toBe(profile);
  await expect.poll(() => new URL(page.url()).searchParams.get('resume')).toBe(sessionId);
  await expect.poll(() => ptyFrames.join('')).toContain('fixture reply 4: browser after cancel');
  await expect(page.getByRole('heading', { name: 'Fixture continuity' })).toBeVisible();
  if (fleet) {
    await page.goto(`${base}/analytics?profile=fleet%3Ainfra`, { waitUntil: 'domcontentloaded' });
    await expect(page.getByText('Total Sessions')).toBeVisible();
    await page.goto(`${base}/config?profile=fleet%3Ainfra`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('code').filter({ hasText: /external-infra\/config.yaml/ })).toBeVisible();
    await page.getByRole('button', { name: 'YAML', exact: true }).click();
    const yaml = page.locator('textarea');
    await expect(yaml).toBeVisible();
    await yaml.fill('display:\n  streaming: true\n');
    await expect(yaml).toHaveValue('display:\n  streaming: true\n');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect.poll(async () => page.evaluate(async () => {
      const response = await fetch('/api/config/raw?profile=fleet%3Ainfra', {
        headers: { 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ },
      });
      return (await response.json()).yaml.includes('streaming: true');
    })).toBe(true);
    const scopedConfig = await page.evaluate(async () => {
      const response = await fetch('/api/config?profile=fleet%3Ainfra', {
        headers: { 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ },
      });
      return (await response.json()).display?.streaming;
    });
    assert.equal(scopedConfig, true);
    await page.goto(`${base}/config?profile=fleet%3Areadonly`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('code').filter({ hasText: /external-readonly\/config.yaml/ })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'YAML', exact: true }).click();
    await expect(page.locator('textarea')).toHaveAttribute('readonly', '');
    const denied = await page.evaluate(async () => {
      const response = await fetch('/api/config/raw?profile=fleet%3Areadonly', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ },
        body: JSON.stringify({ yaml_text: 'display:\n  streaming: true\n' }),
      });
      return response.status;
    });
    assert.equal(denied, 403);
  }
  console.log('authenticated dashboard continuity accepted');
} finally {
  await browser.close();
}
