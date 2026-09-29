import { defineConfig } from '@playwright/test';

// 编排面板（web/ dist）UI E2E。
// 面由 brain_api(7000) 同源托管 web/dist，local 空数据。
// 说明：Windows 下 node spawn 解析 python 的 PATH 不稳，brain 由外部脚本/CI 手动或
// 经 workflow 启动；此处 webServer 用占位命令 + reuseExistingServer 复用手动进程。
export default defineConfig({
  testDir: './e2e',
  testMatch: 'orchest-panel.spec.js',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:7000',
    screenshot: 'only-on-failure',
  },
  webServer: {
    // 占位：本机测试由外部已启动的 brain(7000) 提供；reuseExistingServer 复用。
    command: 'node -e "setTimeout(()=>{}, 120000)"',
    url: 'http://127.0.0.1:7000/stats',
    reuseExistingServer: true,
    timeout: 30_000,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});