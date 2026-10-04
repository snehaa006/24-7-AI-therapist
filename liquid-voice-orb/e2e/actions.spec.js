import { expect, test } from '@playwright/test';
import { installFakes } from './fakes.js';

const ACTION = 'a five-minute walk with your favourite music';
const SUGGESTION = 'That sounds like a lot. Want to try a five-minute walk with your favourite music?';
const CHECKIN = `It's time for ${ACTION}, like we planned. Ready to do it now?`;
const STEPS = [
  'Put on a song you love and stand up slowly.',
  'Start walking at an easy pace, letting your arms swing.',
  'Notice three things you can see as you walk.',
  'Let your breath slow down to match your steps.',
  'Slow down and come to a gentle stop.',
];

const listening = (page) => expect(page.getByText('Listening', { exact: true })).toBeVisible();
const say = (page, text) => page.evaluate((t) => window.__say(t), text);

// ── The whole path, against the real backend (server/tests/e2e_server.py: SQLite, Gemini stubbed) ──

test.describe('full stack', () => {
  test.use({ baseURL: 'http://127.0.0.1:5175' });

  test('suggestion → later → reminder fires → exercise → how did it feel → outcome saved', async ({ page }) => {
    test.setTimeout(180_000);
    await page.addInitScript(installFakes);
    await page.goto('/?fastExercise=1');
    await page.getByRole('button', { name: 'Start Now' }).click();
    await listening(page);
    const api = async (path) => {
      const uid = await page.evaluate(() => localStorage.getItem('therapist-user-id'));
      return (await page.request.get(`${path}?user_id=${uid}`)).json();
    };

    // Too early for a suggestion after one message.
    await say(page, 'work has been really stressful this week');
    await expect(page.locator('.ai-line')).toHaveText('I hear you. Tell me more?');
    await listening(page);

    // Enough said: one action is suggested.
    await say(page, 'my manager keeps piling things on and I cannot switch off');
    await expect(page.locator('.ai-line')).toHaveText(SUGGESTION);
    await listening(page);

    // "Later": the AI confirms the time and a reminder is stored.
    await say(page, 'later, in 1 minute');
    await expect(page.locator('.ai-line')).toHaveText(/^Okay, I'll check in with you in 1 minute\./);
    await expect.poll(() => page.evaluate(() => window.__notifyAsked)).toBe(1);
    const { reminders } = await api('/api/reminders');
    expect(reminders).toHaveLength(1);
    expect(reminders[0].action).toBe(ACTION);
    expect(reminders[0].due_at * 1000 - Date.now()).toBeGreaterThan(50_000);
    await listening(page);

    // A minute later (real time: the in-app timer is what's being tested) the reminder fires:
    // notification, then the check-in.
    await expect(page.locator('.ai-line')).toHaveText(CHECKIN, { timeout: 75_000 });
    expect(await page.evaluate(() => window.__notifications)).toEqual([
      expect.objectContaining({ title: 'Time for your check-in', body: `You planned ${ACTION}.` }),
    ]);
    await listening(page);

    // Yes: the guided exercise runs, each step spoken, with the mic paused.
    await say(page, "yes, let's do it now");
    const panel = page.getByRole('region', { name: 'Guided exercise' });
    await expect(panel.getByText('Walk with your music')).toBeVisible();
    await expect(panel.getByText('Step 1 of 5')).toBeVisible();
    await expect(panel.locator('.exercise-step')).toHaveText(STEPS[0]);
    await expect(panel.getByRole('progressbar')).toBeVisible();
    await expect(panel.getByLabel('Time left in this step')).toHaveText(/^0:0\d$/);
    expect(await page.evaluate(() => window.__rec)).toBeNull(); // not listening during steps
    await expect(panel.getByText('Step 3 of 5')).toBeVisible();
    expect(await page.evaluate(() => window.__rec)).toBeNull();

    // Afterwards: "How did that feel?", and the mic is back on.
    await expect(page.locator('.ai-line')).toHaveText('Nice work. Take a moment before you sit back down. How did that feel?', {
      timeout: 30_000,
    });
    await expect(panel).toHaveCount(0);
    await listening(page);
    const spoken = await page.evaluate(() => window.__spoken.join(' '));
    for (const step of STEPS) expect(spoken).toContain(step);

    await say(page, 'I feel a bit lighter, better actually');
    await expect(page.getByText('I hear you. Tell me more?')).toBeVisible();

    // The outcome is saved, in SQLite and in memory, and the reminder is used up.
    await expect.poll(async () => (await api('/api/outcomes')).outcomes.length).toBe(1);
    const [outcome] = (await api('/api/outcomes')).outcomes;
    expect(outcome).toMatchObject({ action: ACTION, status: 'done', helped: 'yes', words: 'I feel a bit lighter, better actually' });
    expect(outcome.reminder_id).toBe(reminders[0].id);
    const { memories } = await api('/api/memories');
    expect(memories.map((m) => m.text)).toContain(`Tried ${ACTION}: it helped. They said: "I feel a bit lighter, better actually"`);
    expect((await api('/api/reminders')).reminders).toEqual([]);
  });
});

// ── UI details, with /api mocked ─────────────────────────────────────────────

const EXERCISE = {
  title: 'Slow breathing',
  intro: 'Let us slow down together.',
  steps: [
    { say: 'Drop your shoulders.', seconds: 60 },
    { say: 'Breathe in for four and out for six.', seconds: 60 },
    { say: 'Notice your feet on the floor.', seconds: 60 },
    { say: 'Three more slow breaths.', seconds: 60 },
  ],
  closing: 'Well done.',
};

test.describe('mocked API', () => {
  let chats;
  let reminders;

  test.beforeEach(async ({ page }) => {
    chats = [];
    reminders = [];
    await page.addInitScript(installFakes);
    await page.route('**/api/turn', (r) => r.fulfill({ json: { complete: true } }));
    await page.route('**/api/session/**', (r) => r.fulfill({ json: { greeting: null, saved: 0 } }));
    await page.route('**/api/reminders**', (r) => r.fulfill({ json: { reminders } }));
    await page.route('**/api/outcomes', (r) => r.fulfill({ json: {} }));
    await page.route('**/api/chat', (r) => {
      const body = r.request().postDataJSON();
      chats.push(body);
      const last = body.history.at(-1).text;
      if (/die/.test(last)) {
        return r.fulfill({ json: { reply: 'Fixed crisis script.', crisis: true, resources: { helpline_number: '', emergency_number: '112' } } });
      }
      if (/breathing/.test(last)) {
        return r.fulfill({ json: { reply: EXERCISE.intro, action: { type: 'start', action: 'slow breathing', reminder_id: null, exercise: EXERCISE } } });
      }
      if (body.action?.mode === 'checkin') {
        reminders = [];
        return r.fulfill({ json: { reply: "That's okay. What's on your mind?", action: { type: 'declined' } } });
      }
      return r.fulfill({ json: { reply: 'Tell me more?' } });
    });
  });

  test('opening the app with a due reminder starts with the check-in', async ({ page }) => {
    reminders = [{ id: 7, action: 'a short stretch', due_at: Date.now() / 1000 - 120 }];
    await page.goto('/');
    await expect(page.getByRole('status')).toContainText("It's time for a short stretch.");
    await page.getByRole('button', { name: 'Start Now' }).click();
    await expect(page.locator('.ai-line')).toHaveText("Hi, welcome back. It's time for a short stretch, like we planned. Ready to do it now?");
    await listening(page);
    await say(page, 'no, not today');
    await expect(page.locator('.ai-line')).toHaveText("That's okay. What's on your mind?");
    expect(chats[0].action).toEqual({ mode: 'checkin', action: 'a short stretch', reminder_id: 7 });
    expect(chats[0].tz_offset).toEqual(expect.any(Number));
  });

  test('only one suggestion check per session, and none after a crisis', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Start Now' }).click();
    await listening(page);
    await say(page, 'I had a long day');
    await expect(page.locator('.ai-line')).toHaveText('Tell me more?');
    expect(chats[0].action).toEqual({ mode: 'consider' });
    await listening(page);
    await say(page, 'sometimes I want to die');
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await listening(page);
    await say(page, 'I am still here');
    await expect.poll(() => chats.length).toBe(3);
    expect(chats[2].action).toBeNull();
  });

  test('pause, resume and stop an exercise', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Start Now' }).click();
    await listening(page);
    await say(page, 'yes some breathing please');
    const panel = page.getByRole('region', { name: 'Guided exercise' });
    await expect(panel.getByText('Step 1 of 4')).toBeVisible();
    await expect(page.getByLabel('Type a message')).toBeHidden(); // mic and typing are off during steps

    await panel.getByRole('button', { name: 'Pause' }).click();
    const timer = panel.getByLabel('Time left in this step');
    const held = await timer.textContent();
    await page.waitForTimeout(1500);
    await expect(timer).toHaveText(held);
    await panel.getByRole('button', { name: 'Resume' }).click();
    await expect(timer).not.toHaveText(held);

    await panel.getByRole('button', { name: 'Stop' }).click();
    await expect(panel).toHaveCount(0);
    await expect(page.locator('.ai-line')).toHaveText("That's okay, we can stop there. How are you feeling now?");
    await listening(page);
    await say(page, 'a little calmer');
    await expect.poll(() => chats.length).toBe(2);
    expect(chats[1].action).toEqual({ mode: 'feedback', action: 'slow breathing', reminder_id: null, done: false });
  });

  /** 0.2 s of silence, 16-bit mono 24 kHz, as /api/speak returns. */
  function silentWav() {
    const samples = 4800;
    const buf = Buffer.alloc(44 + samples * 2);
    buf.write('RIFF', 0);
    buf.writeUInt32LE(36 + samples * 2, 4);
    buf.write('WAVEfmt ', 8);
    buf.writeUInt32LE(16, 16);
    buf.writeUInt16LE(1, 20);
    buf.writeUInt16LE(1, 22);
    buf.writeUInt32LE(24000, 24);
    buf.writeUInt32LE(48000, 28);
    buf.writeUInt16LE(2, 32);
    buf.writeUInt16LE(16, 34);
    buf.write('data', 36);
    buf.writeUInt32LE(samples * 2, 40);
    return buf;
  }

  test('natural voice: the next step is generated while the current one runs', async ({ page }) => {
    const asked = [];
    await page.addInitScript(() =>
      localStorage.setItem('voice-settings-v1', JSON.stringify({ source: 'natural', naturalVoice: 'Sulafat', pacing: 'quick' }))
    );
    await page.route('**/api/speak', (r) => {
      asked.push(r.request().postDataJSON().text);
      r.fulfill({ body: silentWav(), contentType: 'audio/wav' });
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Start Now' }).click();
    await listening(page);
    await say(page, 'breathing');
    const panel = page.getByRole('region', { name: 'Guided exercise' });
    await expect(panel.getByText('Step 1 of 4')).toBeVisible();
    await expect.poll(() => asked).toContain(EXERCISE.steps[1].say);
    await expect(panel.getByText('Step 1 of 4')).toBeVisible(); // still on step 1
  });
});
