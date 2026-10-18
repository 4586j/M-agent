import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'dev-proxy.spec.js',
  workers: 1,
  timeout: 30_000,
  use: { baseURL: 'http://127.0.0.1:8128', screenshot: 'off' },
  webServer: [
    {
      command: 'node e2e/fixtures/proxy-brain.cjs',
      url: 'http://127.0.0.1:8129/health',
      reuseExistingServer: false,
    },
    {
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 8128 --strictPort',
      env: { ORCH_BRAIN_URL: 'http://127.0.0.1:8129' },
      url: 'http://127.0.0.1:8128',
      reuseExistingServer: false,
    },
  ],
});
