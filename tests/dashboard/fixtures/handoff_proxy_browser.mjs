import assert from 'node:assert/strict';
import { chromium, expect } from '@playwright/test';

const [base, chromePath] = process.argv.slice(2);
const browser = await chromium.launch({
  ...(chromePath ? { executablePath: chromePath } : {}),
  headless: true,
  args: ['--no-sandbox'],
});

async function openSaved(context, owner, sessionId) {
  const page = await context.newPage({ viewport: { width: 1440, height: 900 } });
  const frames = [];
  page.routeWebSocket('**/api/ws*', socket => socket.close());
  page.on('websocket', socket => {
    if (socket.url().includes('/api/pty?')) {
      socket.on('framereceived', frame => frames.push(String(frame.payload)));
    }
  });
  await page.goto(`${base}/sessions`, { waitUntil: 'domcontentloaded' });
  await expect.poll(async () => (await context.cookies(base))
    .find(cookie => cookie.name === 'hermes-handoff-owner')?.value).toBe(owner);
  const switcher = page.locator('#hermes-profile-switcher');
  await expect(switcher).toBeVisible();
  await switcher.locator('button').click();
  await switcher.getByRole('option', { name: 'acceptance' }).click();
  await page.getByRole('link', { name: 'Chat', exact: true }).click();
  await page.getByText(`${owner === 'old' ? 'Old' : 'Candidate'} saved`, { exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('resume')).toBe(sessionId);
  const terminal = page.locator('.hermes-chat-xterm-host');
  await expect(terminal.getByRole('textbox', { name: 'Terminal input' })).toBeVisible();
  await expect.poll(() => frames.join('')).toContain(`${owner} saved answer`);
  await expect.poll(() => frames.join('')).toContain('fixture ready>');
  return { page, frames, terminal };
}

async function sendTurn(view, text, replyNumber, assertStreaming = false) {
  view.frames.length = 0;
  await view.terminal.click();
  await view.page.keyboard.type(text);
  await view.page.keyboard.press('Enter');
  const reply = `fixture reply ${replyNumber}: ${text}`;
  if (assertStreaming) {
    await expect.poll(() => view.frames.join('')).toContain('fixture ');
    assert.ok(!view.frames.join('').includes(reply),
      'the full reply arrived before the browser observed the first chunk');
  }
  await expect.poll(() => view.frames.join('')).toContain(reply);
  await expect.poll(() => view.frames.join('')).toContain('fixture ready>');
  if (assertStreaming) {
    let prefix = '';
    const sawPartialReply = view.frames.some(frame => {
      prefix += frame;
      return prefix.includes('fixture ') && !prefix.includes(reply);
    });
    assert.ok(sawPartialReply, `reply was buffered into one WebSocket frame: ${JSON.stringify(view.frames)}`);
  }
}

try {
  const oldContext = await browser.newContext();
  await oldContext.addInitScript(() => {
    localStorage.setItem('hermes.pty.token.chat', 'fixture-old-attach');
  });
  const unauthenticated = await oldContext.request.get(`${base}/api/sessions?profile=acceptance`);
  assert.equal(unauthenticated.status(), 401);
  const old = await openSaved(oldContext, 'old', 'old-saved');
  await sendTurn(old, 'old turn one', 2);

  const candidateContext = await browser.newContext();
  const candidate = await openSaved(candidateContext, 'candidate', 'candidate-saved');
  await sendTurn(candidate, 'candidate turn one', 2, true);

  candidate.frames.length = 0;
  await candidate.terminal.click();
  await candidate.page.keyboard.type('slow fixture turn');
  await candidate.page.keyboard.press('Enter');
  await expect.poll(() => candidate.frames.join('')).toContain('fixture pending');
  await candidate.page.keyboard.press('Control+C');
  await expect.poll(() => candidate.frames.join('')).toContain('fixture cancelled');

  await sendTurn(old, 'old during candidate', 3);
  old.frames.length = 0;
  await old.page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => new URL(old.page.url()).searchParams.get('resume')).toBe('old-saved');
  await expect.poll(() => old.frames.join('')).toContain('fixture reply 3: old during candidate');
  await sendTurn(old, 'old after candidate', 4);
  assert.equal((await oldContext.cookies(base)).find(c => c.name === 'hermes-handoff-owner')?.value, 'old');
  assert.equal((await candidateContext.cookies(base)).find(c => c.name === 'hermes-handoff-owner')?.value, 'candidate');
  console.log('disposable real-dashboard proxy browser continuity accepted');
} finally {
  await browser.close();
}
