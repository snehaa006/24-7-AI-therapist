import { expect, test } from '@playwright/test';
import { installFakes } from './fakes.js';

const RESOURCES = {
  helpline_name: 'Test Lifeline',
  helpline_number: '0800 123',
  emergency_number: '999',
  directory_url: 'https://findahelpline.com',
};
const SCRIPT = "I'm really glad you told me. Please call Test Lifeline on 0800 123, or 999 if you're in danger.";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(installFakes);
  await page.route('**/api/turn', (r) => r.fulfill({ json: { complete: true } }));
  await page.route('**/api/session/**', (r) => r.fulfill({ json: { greeting: null, saved: 0 } }));
  await page.route('**/api/chat', async (route) => {
    const { history } = route.request().postDataJSON();
    const last = history.at(-1).text;
    if (/die/.test(last)) {
      return route.fulfill({
        json: { reply: SCRIPT, speech: SCRIPT.replace('0800 123', '0 8 0 0 1 2 3'), crisis: true, resources: RESOURCES },
      });
    }
    return route.fulfill({ json: { reply: `You said: ${last}. Tell me more?`, crisis: false } });
  });
});

async function startSession(page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Start Now' }).click();
  await expect(page.getByText('Listening', { exact: true })).toBeVisible();
}

test('a normal message gets an AI reply and no crisis card', async ({ page }) => {
  await startSession(page);
  await page.evaluate(() => window.__say('this traffic is killing me'));
  await expect(page.locator('.ai-line')).toHaveText('You said: this traffic is killing me. Tell me more?');
  await expect(page.locator('.crisis-card')).toHaveCount(0);
});

test('a crisis message speaks the fixed script and shows tap-to-call links', async ({ page }) => {
  await startSession(page);
  await page.evaluate(() => window.__say('I want to die'));

  const card = page.getByRole('alertdialog');
  await expect(card).toBeVisible();
  await expect(card.getByRole('link', { name: /Test Lifeline.*0800 123/ })).toHaveAttribute('href', 'tel:0800123');
  await expect(card.getByRole('link', { name: /999/ })).toHaveAttribute('href', 'tel:999');
  await expect(page.locator('.ai-line')).toHaveText(SCRIPT);
  await expect.poll(() => page.evaluate(() => window.__spoken.join(' '))).toContain('0 8 0 0 1 2 3');

  // Still listening afterwards, and the card can be closed.
  await expect(page.getByText('Listening', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Keep talking here' }).click();
  await expect(card).toHaveCount(0);
});

test('typed messages go through the same check', async ({ page }) => {
  await startSession(page);
  await page.getByLabel('Type a message').fill('I want to die');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('alertdialog')).toBeVisible();
});
