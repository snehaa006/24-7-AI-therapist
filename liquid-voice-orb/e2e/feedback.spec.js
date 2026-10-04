import { expect, test } from '@playwright/test';
import { installFakes } from './fakes.js';

// Step 6, against the real backend (server/tests/e2e_server.py: SQLite, Gemini stubbed).
test.use({ baseURL: 'http://127.0.0.1:5175' });

const listening = (page) => expect(page.getByText('Listening', { exact: true })).toBeVisible();
const say = (page, text) => page.evaluate((t) => window.__say(t), text);
const aiLine = (page) => page.locator('.ai-line');

test('breathing didn’t help, a walk did: the next suggestion is a walk, and “What helps” shows it', async ({ page }) => {
  test.setTimeout(180_000);
  await page.addInitScript(installFakes);
  await page.goto('/?fastExercise=1');
  const api = async (path) => {
    const uid = await page.evaluate(() => localStorage.getItem('therapist-user-id'));
    return (await page.request.get(`${path}?user_id=${uid}`)).json();
  };
  await page.getByRole('button', { name: 'Start Now' }).click();
  await listening(page);
  const panel = page.getByRole('region', { name: 'Guided exercise' });

  // Asked for by name: it runs straight away, with no suggestion first.
  await say(page, 'can we do breathing?');
  await expect(panel.getByText('Slow breathing')).toBeVisible();
  await expect(aiLine(page)).toHaveText('Well done. Take a moment before you carry on. How did that feel?', { timeout: 40_000 });
  await listening(page);
  await say(page, "honestly it didn't help");
  await expect(aiLine(page)).toHaveText('I hear you. Tell me more?');
  await listening(page);

  // A second ask in the same session still works.
  await say(page, "let's go for a walk");
  await expect(panel.getByText('Walk with your music')).toBeVisible();
  await expect(aiLine(page)).toHaveText('Nice work. Take a moment before you sit back down. How did that feel?', { timeout: 40_000 });
  await listening(page);
  await say(page, 'I feel better');
  await expect(aiLine(page)).toHaveText('I hear you. Tell me more?');

  // Both outcomes are stored with their type; the stats are worked out in code.
  await expect.poll(async () => (await api('/api/outcomes')).outcomes.length).toBe(2);
  const { outcomes } = await api('/api/outcomes');
  expect(outcomes.map((o) => [o.type, o.helped])).toEqual([
    ['walk', 'yes'],
    ['breathing', 'no'],
  ]);
  const stats = await api('/api/stats');
  expect(stats.next).toBe('walk');
  expect(stats.types[0]).toMatchObject({ type: 'walk', summary: 'helped 1 of 1' });

  // Next session: the suggestion is a walk.
  await page.getByRole('button', { name: 'End', exact: true }).click();
  await page.getByRole('button', { name: 'Start Now' }).click();
  await listening(page);
  await say(page, 'work has been really stressful this week');
  await expect(aiLine(page)).toHaveText('I hear you. Tell me more?');
  await listening(page);
  await say(page, 'my manager keeps piling things on and I cannot switch off');
  await expect(aiLine(page)).toHaveText('That sounds like a lot. Want to try a five-minute walk with your favourite music?');

  // "What helps" in the Memories panel, and resetting it.
  await page.getByRole('button', { name: 'End', exact: true }).click();
  await page.getByRole('button', { name: 'Memories' }).click();
  const helps = page.getByRole('region', { name: 'What helps' });
  await expect(helps.getByRole('listitem')).toHaveText([/^Walk – helped 1 of 1\s*today$/, /^Breathing – helped 0 of 1\s*today$/]);
  await helps.getByRole('button', { name: 'Reset what helps' }).click();
  await helps.getByRole('button', { name: 'Tap again to reset what helps' }).click();
  await expect(helps).toHaveCount(0);
  expect((await api('/api/stats')).types.every((t) => t.tries === 0)).toBe(true);
  expect((await api('/api/memories')).memories.filter((m) => /^Tried /.test(m.text))).toEqual([]);
});
