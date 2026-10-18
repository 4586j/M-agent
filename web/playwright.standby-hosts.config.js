import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e',testMatch:'standby-hosts.spec.js',workers:1,timeout:30000,
  use:{viewport:{width:1280,height:900},screenshot:'off',video:'off'},
  projects:[
    {name:'personal',use:{baseURL:'http://127.0.0.1:8141'}},
    {name:'orchestration',use:{baseURL:'http://127.0.0.1:8142'}},
  ],
  webServer:[
    {command:'python -m agent_core.service --port 8141',cwd:'..',
      env:{PYTHONUTF8:'1',AGENT_RATE_LIMIT_PER_MINUTE:'2000',AGENT_ENV_FILE:'.tmp/no-e2e-env',AGENT_WORK_DIR:'.tmp/standby-host-e2e'},
      url:'http://127.0.0.1:8141/health',reuseExistingServer:false,timeout:30000},
    {command:'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 8142 --strictPort',
      url:'http://127.0.0.1:8142',reuseExistingServer:false},
  ],
});
