import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// 编排面板（web/ dist）UI E2E。面由 brain_api(7000) 同源托管 web/dist，local 空数据。
// webServer 双态：
//  - CI 环境：起真实 brain（uvicorn 托管 build 后的 dist）
//  - 本地 Windows：占位 + reuseExistingServer（node 解析 python 的 PATH 在此环境不稳，
//    由外部手动起 brain 提供 7000）
const webDir = path.dirname(fileURLToPath(import.meta.url));
const runtimeDir = path.resolve(webDir, '../workspace/agent/orchestrator/runtime');
const distDir = path.resolve(webDir, 'dist');

const realServerCommand =
  `bash -c 'cd ${runtimeDir} && ` +
  `ORCH_MODE=local ` +
  `ORCH_TASK_STORE_DIR=${runtimeDir}/.e2e_ci_tasks ` +
  `ORCH_WEB_DIST=${distDir} ` +
  `python -m uvicorn brain_api:_build_app --factory --port 7000 --log-level warning'`;

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
    command: process.env.CI ? realServerCommand : 'node -e "setTimeout(()=>{}, 120000)"',
    url: 'http://127.0.0.1:7000/stats',
    reuseExistingServer: true,
    timeout: 30_000,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});
