import { defineConfig } from '@playwright/test';

// 布局与会话隔离回归使用 mock API；Vite 仅提供源码，不连接真实 Brain 或模型。
export default defineConfig({
  testDir: './e2e',
  testMatch: ['layout-regressions.spec.js', 'brain-session-isolation.spec.js', 'brain-view-isolation.spec.js', 'understanding-gate.spec.js'],
  workers: 1,
  timeout: 30_000,
  use: { baseURL: 'http://127.0.0.1:8126', viewport: { width: 1280, height: 900 }, screenshot: 'off' },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 8126 --strictPort',
    url: 'http://127.0.0.1:8126',
    reuseExistingServer: false,
  },
});
