// 编排面板（web/）核心 UI E2E。
// 面由 brain_api 同源托管 web/dist（local 模式空数据），测 8 个视图渲染 + 导航 + 无 JS 错误。
import { test, expect } from '@playwright/test';

const ERR = [];

test.beforeEach(async ({ page }) => {
  ERR.length = 0;
  page.on('pageerror', (e) => ERR.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') ERR.push(m.text()); });
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(600);
});

test.describe('编排面板：导航与视图', () => {
  test('加载无 JS 错误，顶栏导航就绪', async ({ page }) => {
    await expect(page.locator('header')).toContainText('编排控制面板');
    for (const name of ['总览', '任务', '节点', '命令台', '记忆', '调度', '审计', 'Provider']) {
      await expect(page.getByRole('link', { name })).toBeVisible();
    }
    expect(ERR).toEqual([]);
  });

  test('七个视图路由可达且渲染空态', async ({ page }) => {
    const views = [
      ['任务', '/tasks'],
      ['节点', '/nodes'],
      ['命令台', '/command'],
      ['记忆', '/memory'],
      ['调度', '/schedules'],
      ['审计', '/audit'],
    ];
    for (const [label, path] of views) {
      await page.getByRole('link', { name: label }).first().click();
      await page.waitForURL(`**${path}`);
      await page.waitForTimeout(400);
      expect(ERR).toEqual([]);  // 每个视图都应无 JS 错误
    }
    // 回总览
    await page.getByRole('link', { name: '总览' }).click();
    await page.waitForURL('**/');
  });

  test('任务页筛选栏与空列表渲染', async ({ page }) => {
    await page.getByRole('link', { name: '任务' }).first().click();
    await page.waitForURL('**/tasks');
    await expect(page.getByPlaceholder(/搜索任务/)).toBeVisible();
    await expect(page.getByText('没有匹配的任务')).toBeVisible();
  });

  test('调度页 cron 预览可用', async ({ page }) => {
    await page.getByRole('link', { name: '调度' }).click();
    await page.waitForURL('**/schedules');
    // 空态 + 新建按钮
    await expect(page.getByText('暂无调度任务')).toBeVisible();
    await page.getByRole('button', { name: '+ 新建调度' }).click();
    await expect(page.getByPlaceholder(/cron|名称/).last()).toBeVisible();
  });

  test('记忆页四 Tab 渲染', async ({ page }) => {
    await page.getByRole('link', { name: '记忆' }).click();
    await page.waitForURL('**/memory');
    for (const tab of ['核心记忆', '任务上下文', '经验库', '演化提案']) {
      await expect(page.getByRole('button', { name: tab })).toBeVisible();
    }
    expect(ERR).toEqual([]);
  });
});

test.describe('编排面板：功能部件', () => {
  test('命令台表单组件渲染', async ({ page }) => {
    await page.getByRole('link', { name: '命令台' }).first().click();
    await page.waitForURL('**/command');
    await expect(page.getByRole('textbox').first()).toBeVisible();  // prompt
    await expect(page.getByRole('button', { name: /派发/ })).toBeVisible();
  });

  test('审计页过滤控件渲染', async ({ page }) => {
    await page.getByRole('link', { name: '审计' }).click();
    await page.waitForURL('**/audit');
    await expect(page.getByText('安全裁决流水')).toBeVisible();
    // 空态或记录均可
    await expect(page.locator('main')).toBeVisible();
  });
});