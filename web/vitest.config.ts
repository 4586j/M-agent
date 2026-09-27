import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// 前端单元测试配置（docs/测试设计.md §3.1）：jsdom + RTL；CSS 关闭加速
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
