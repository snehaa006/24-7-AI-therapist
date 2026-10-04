import { existsSync } from 'node:fs';
import { defineConfig } from '@playwright/test';

// The server's virtualenv if there is one (see README), else whatever `python3` is.
const PYTHON = process.env.PYTHON || (existsSync('../server/.venv/bin/python') ? '../server/.venv/bin/python' : 'python3');

// UI tests with the browser's speech APIs faked (e2e/fakes.js) and /api mocked per test.
// The step 5 test (e2e/actions.spec.js) also runs against the real backend, with Gemini stubbed,
// through a second dev server on :5175.
export default defineConfig({
  testDir: 'e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://127.0.0.1:5174',
    browserName: 'chromium',
    viewport: { width: 390, height: 760 }, // small canvas: the orb renders in software WebGL here
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
  },
  webServer: [
    {
      command: 'npx vite --port 5174 --strictPort --host 127.0.0.1',
      url: 'http://127.0.0.1:5174',
      reuseExistingServer: false,
    },
    {
      command: `${PYTHON} ../server/tests/e2e_server.py 8011`,
      url: 'http://127.0.0.1:8011/api/health',
      reuseExistingServer: false,
    },
    {
      command: 'npx vite --port 5175 --strictPort --host 127.0.0.1',
      url: 'http://127.0.0.1:5175',
      env: { API_PORT: '8011' },
      reuseExistingServer: false,
    },
  ],
});
