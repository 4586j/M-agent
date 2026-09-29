import { defineConfig } from '@playwright/test';

// Agent 面板（agent_core/panel）UI E2E。
// 依赖 agent_core.service 提供 /panel 与 WebSocket 端点（webServer 自动拉起）。
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8100',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'python -m agent_core.service --port 8100',
    url: 'http://127.0.0.1:8100/health',
    reuseExistingServer: true,
    timeout: 20_000,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});