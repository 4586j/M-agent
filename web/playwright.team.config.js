import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './e2e', testMatch: 'team-planning.spec.js', workers: 1, timeout: 30000,
  use: {screenshot: 'off', video: 'off'},
});
