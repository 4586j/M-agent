import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './e2e', testMatch: 'standby-lab.spec.js', workers: 1, timeout: 30000,
  use: {baseURL: 'http://127.0.0.1:7079', screenshot: 'off', video: 'off'},
  webServer: {
    command: 'python -m http.server 7079 --bind 127.0.0.1 --directory web/public',
    cwd: '..', url: 'http://127.0.0.1:7079/standby-lab/', reuseExistingServer: false,
  },
});
