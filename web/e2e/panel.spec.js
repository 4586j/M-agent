// Agent 面板核心 UI 校验（不依赖真实模型——纯前端渲染/交互契约）。
import { test, expect } from '@playwright/test';

const JS_ERRORS = [];

test.beforeEach(async ({ page }) => {
  JS_ERRORS.length = 0;
  page.on('pageerror', (err) => JS_ERRORS.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') JS_ERRORS.push(msg.text());
  });
  await page.goto('/panel');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(800);
});

test.describe('Agent 面板核心', () => {
  test('加载无 JS 错误，三栏布局齐全', async ({ page }) => {
    await expect(page.locator('#sidebar')).toBeVisible();
    await expect(page.locator('#stream')).toBeVisible();
    await expect(page.locator('#rightbar')).toBeVisible();
    await expect(page.locator('#ws-chip')).toBeVisible();
    await expect(page.locator('#composer')).toBeVisible();
    expect(JS_ERRORS).toEqual([]);
  });

  test('视图 Tab 切换：事件流 / 终端', async ({ page }) => {
    const stream = page.locator('#stream');
    const termView = page.locator('#term-view');
    await expect(stream).toBeVisible();               // 默认事件流
    await page.getByRole('button', { name: /终端/ }).click();
    await expect(termView).toHaveClass(/show/);       // 切到终端
    await expect(page.locator('#composer')).toBeHidden();  // 输入框隐藏
    await page.getByRole('button', { name: /事件流/ }).click();
    await expect(page.locator('#composer')).toBeVisible(); // 切回
  });

  test('终端视图：输入框、cwd 状态栏、清屏按钮就绪', async ({ page }) => {
    await page.getByRole('button', { name: /终端/ }).click();
    await page.waitForTimeout(1200);  // 等 WS ready
    await expect(page.locator('#term-in')).toBeVisible();
    // cwd 应已由 ready 帧填充（非"未连接"初始态）
    await expect(page.locator('#term-cwd')).not.toHaveText('未连接');
    await expect(page.locator('#term-cwd')).not.toBeEmpty();
    await expect(page.getByRole('button', { name: '清屏' })).toBeVisible();
  });

  test('工作区 chip 显示当前工作区', async ({ page }) => {
    await expect(page.locator('#ws-name')).not.toHaveText('—');
  });

  test('布局菜单可打开并含栏目显隐项', async ({ page }) => {
    await page.locator('.btn-ico').click();
    await expect(page.locator('#menu-pop')).toHaveClass(/open/);
    await expect(page.locator('#tg-left')).toBeVisible();
    await expect(page.locator('#tg-right')).toBeVisible();
    await expect(page.locator('#menu-pop')).toContainText('重置栏宽');
  });

  test('隐藏左栏后中栏占满', async ({ page }) => {
    await page.locator('.btn-ico').click();
    await page.locator('#tg-left').uncheck();
    await page.waitForTimeout(200);
    await expect(page.locator('#sidebar')).toBeHidden();
    await expect(page.locator('#stream')).toBeVisible();
    // 复位
    await page.locator('#tg-left').check();
  });

  test('左栏运行历史区域存在', async ({ page }) => {
    await expect(page.locator('#runlist')).toBeVisible();
  });
});