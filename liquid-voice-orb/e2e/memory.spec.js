import http from 'node:http';
import { expect, test } from '@playwright/test';
import { installFakes } from './fakes.js';

const DEFAULT_GREETING = "Hi, I'm here, and I'm listening. What's on your mind today?";
const RETURNING = 'Hi again. Last time you were nervous about the bakery interview. How did it go?';

let calls;

test.beforeEach(async ({ page, context }) => {
  calls = { start: [], end: [], chat: [], deleted: [] };
  let memories = [
    { id: 1, kind: 'fact', text: 'Has a job interview at a bakery on Friday.' },
    { id: 2, kind: 'pattern', text: 'Gets nervous before big events.' },
  ];
  await page.addInitScript(installFakes);
  await page.route('**/api/reminders**', (r) => r.fulfill({ json: { reminders: [] } }));
  // Watched as requests, not routes: a sendBeacon sent while the page unloads isn't routed.
  context.on('request', (req) => {
    if (req.url().endsWith('/api/session/end') && req.resourceType() === 'fetch') calls.end.push(req.postDataJSON());
  });
  await page.route('**/api/session/end', (r) => r.fulfill({ json: { saved: 1 } }));
  await page.route('**/api/turn', (r) => r.fulfill({ json: { complete: true } }));
  await page.route('**/api/session/start', (r) => {
    calls.start.push(r.request().postDataJSON());
    r.fulfill({ json: { greeting: calls.greeting ?? null } });
  });
  await page.route('**/api/chat', (r) => {
    const body = r.request().postDataJSON();
    calls.chat.push(body);
    const last = body.history.at(-1).text;
    if (/die/.test(last)) {
      return r.fulfill({ json: { reply: 'Fixed crisis script.', crisis: true, resources: { helpline_number: '', emergency_number: '112', directory_url: 'https://findahelpline.com' } } });
    }
    r.fulfill({ json: { reply: 'It went well? Tell me more.', crisis: false } });
  });
  await page.route('**/api/memories**', (r) => {
    const req = r.request();
    const url = new URL(req.url());
    if (req.method() === 'GET') return r.fulfill({ json: { memories, last_summary: 'Talked about the bakery interview.' } });
    const id = url.pathname.split('/').at(-1);
    calls.deleted.push({ id, user_id: url.searchParams.get('user_id') });
    memories = id === 'memories' ? [] : memories.filter((m) => String(m.id) !== id);
    return r.fulfill({ json: { ok: true } });
  });
});

async function startSession(page) {
  await page.getByRole('button', { name: 'Start Now' }).click();
  await expect(page.getByText('Listening', { exact: true })).toBeVisible();
}

test('a returning user is greeted with something from last time', async ({ page }) => {
  calls.greeting = RETURNING;
  await page.goto('/');
  await startSession(page);
  await expect(page.locator('.ai-line')).toHaveText(RETURNING);
  expect(await page.evaluate(() => window.__spoken.join(' '))).toContain('bakery interview');

  // The same anonymous id is used for the greeting and every reply, and survives a reload.
  const id = calls.start[0].user_id;
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  await page.evaluate(() => window.__say('it went really well'));
  await expect(page.locator('.ai-line')).toHaveText('It went well? Tell me more.');
  expect(calls.chat[0].user_id).toBe(id);
  // The greeting is part of the history, so the reply can follow on from it.
  expect(calls.chat[0].history[0]).toEqual({ role: 'assistant', text: RETURNING });
  await page.reload();
  expect(await page.evaluate(() => localStorage.getItem('therapist-user-id'))).toBe(id);
});

test('a new user gets the usual greeting', async ({ page }) => {
  await page.goto('/');
  await startSession(page);
  await expect(page.locator('.ai-line')).toHaveText(DEFAULT_GREETING);
});

test('End saves the session once, with crisis turns flagged', async ({ page }) => {
  await page.goto('/');
  await startSession(page);
  await page.evaluate(() => window.__say('I have an interview on Friday'));
  await expect(page.locator('.ai-line')).toHaveText('It went well? Tell me more.');
  await expect(page.getByText('Listening', { exact: true })).toBeVisible();
  await page.evaluate(() => window.__say('sometimes I want to die'));
  await expect(page.getByRole('alertdialog')).toBeVisible();

  await page.getByRole('button', { name: 'End', exact: true }).click();
  await expect.poll(() => calls.end.length).toBe(1);
  const [saved] = calls.end;
  expect(saved.user_id).toBe(calls.start[0].user_id);
  expect(saved.session_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(saved.history.filter((t) => t.crisis).map((t) => t.role)).toEqual(['user', 'assistant']);
  expect(saved.history.find((t) => t.text === 'I have an interview on Friday').crisis).toBeUndefined();

  // Leaving the page afterwards doesn't send it again.
  await page.goto('about:blank');
  await page.goto('/');
  expect(await page.evaluate(() => localStorage.getItem('__beacons'))).toBeNull();
  expect(calls.end).toHaveLength(1);
});

test('closing the tab mid-session saves it with sendBeacon', async ({ page }) => {
  // While Playwright intercepts requests, a beacon sent during unload gets aborted. So routes
  // are removed before leaving, and the beacon goes through Vite's /api proxy to a stand-in
  // for the backend on :8000, which reads the body.
  const beacons = [];
  const backend = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      if (req.url === '/api/session/end') beacons.push({ type: req.headers['content-type'], ...JSON.parse(body) });
      res.end('{}');
    });
  });
  await new Promise((r) => backend.listen(8000, '127.0.0.1', r));
  try {
    await page.goto('/');
    await startSession(page);
    await page.evaluate(() => window.__say('my sister Maya is visiting'));
    await expect(page.locator('.ai-line')).toHaveText('It went well? Tell me more.');
    await page.unrouteAll();
    await page.goto('about:blank');
    await expect.poll(() => beacons.length).toBe(1);
    expect(beacons[0].type).toBe('application/json');
    expect(beacons[0].history.some((t) => t.text === 'my sister Maya is visiting')).toBe(true);
    expect(calls.end).toHaveLength(0); // it was the beacon, not a fetch
  } finally {
    await new Promise((r) => backend.close(r));
  }
});

test('a session with nothing said is not saved', async ({ page }) => {
  await page.goto('/');
  await startSession(page);
  await page.getByRole('button', { name: 'End', exact: true }).click();
  await page.waitForTimeout(500);
  expect(calls.end).toHaveLength(0);
});

test('the memory panel lists and deletes memories', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Memories' }).click();
  const panel = page.getByRole('dialog', { name: 'Memories' });
  await expect(panel.getByText('Talked about the bakery interview.')).toBeVisible();
  await expect(panel.getByText('Gets nervous before big events.')).toBeVisible();

  await panel.getByRole('button', { name: 'Forget: Has a job interview at a bakery on Friday.' }).click();
  await expect(panel.getByText('Has a job interview at a bakery on Friday.')).toHaveCount(0);
  expect(calls.deleted[0].id).toBe('1');
  expect(calls.deleted[0].user_id).toBe(await page.evaluate(() => localStorage.getItem('therapist-user-id')));

  await panel.getByRole('button', { name: 'Forget everything' }).click();
  await panel.getByRole('button', { name: 'Tap again to forget everything' }).click();
  await expect(panel.getByText(/Nothing yet/)).toBeVisible();
  expect(calls.deleted.at(-1).id).toBe('memories');
});
