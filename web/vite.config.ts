import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev：把后端 API 路径转发到 brain_api（7000），前端用相对路径即可
// prod：构建产物由 brain_api 同源托管，无需代理
const brainTarget = process.env.ORCH_BRAIN_URL || 'http://127.0.0.1:7000'
const apiPaths = [
  '/workers', '/tasks', '/dispatch', '/register', '/heartbeat', '/stats', '/proxy',
  // 二期：记忆系统 / 程序记忆演化 / 经验库 / 顾问与翻译 / 调度与审计（三期）
  '/memory', '/experiences', '/recommend', '/translate', '/schedules', '/audit',
]

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: brainTarget, changeOrigin: true }])),
  },
})
