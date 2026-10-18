import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e', testMatch:'personal-understanding.spec.js',workers:1,timeout:30000,
  use:{baseURL:'http://127.0.0.1:8130',screenshot:'off'},
  webServer:{command:'python -m agent_core.service --port 8130',cwd:'..',
    env:{PYTHONUTF8:'1',AGENT_RATE_LIMIT_PER_MINUTE:'600',AGENT_ENV_FILE:'.tmp/no-e2e-env',AGENT_WORK_DIR:'.tmp/personal-learning-e2e'},
    url:'http://127.0.0.1:8130/health',reuseExistingServer:false,timeout:30000},
});
