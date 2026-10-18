import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname;
    const task = {task_id:'layout-task',role:'executor',prompt:'布局回归任务',status:'done',
      output:'完成',artifacts:[],created_at:'1',updated_at:'1',worker_id:null};
    let data;
    if(path === '/stats') data = {mode:'local',tasks:{done:1},workers:{},cost:{}};
    else if(path === '/tasks') data = [task];
    else if(path.startsWith('/tasks/')) data = task;
    else if(path === '/workers') data = [];
    else if(path === '/budgets') data = {global_limit_usd:null,daily_limit_usd:null,spent_total_usd:0,spent_today_usd:0};
    else if(path === '/memory/core') data = [];
    else return route.continue();
    await route.fulfill({json:data});
  });
  await page.goto('/#/tasks');
  await expect(page.getByRole('button',{name:/layout-task/})).toBeVisible();
});

test('任务详情覆盖整个视口，关闭和遮罩仍可操作', async ({ page }) => {
  await page.getByRole('button',{name:/layout-task/}).click();
  const drawer = page.getByRole('dialog',{name:'任务详情'});
  await expect(drawer).toBeVisible();
  await expect.poll(async () => drawer.evaluate(el => {
    const rect = el.getBoundingClientRect();
    return Math.abs(rect.top) + Math.abs(rect.height-innerHeight) + Math.abs(rect.right-innerWidth);
  })).toBeLessThan(2);
  expect(await drawer.evaluate(el => el.parentElement === document.body)).toBe(true);
  await drawer.getByRole('button',{name:'关闭',exact:true}).click();
  // 遮罩关闭不影响任务列表继续打开详情。
  await page.getByRole('button',{name:/layout-task/}).click();
  await expect(drawer).toBeVisible();
  await page.locator('body > div[aria-hidden="true"].fixed').click({position:{x:20,y:100}});
  await expect(drawer).toBeHidden();
});

test('导航滑块跟随实际选中项，跨分组也保持对齐', async ({ page }) => {
  const error = () => page.locator('nav').evaluate(nav => {
    const selected = nav.querySelector('[aria-current="page"]').getBoundingClientRect();
    const pill = nav.querySelector('[data-testid="nav-indicator"]').getBoundingClientRect();
    return Math.abs(selected.top-pill.top)+Math.abs(selected.height-pill.height);
  });
  await expect(page.getByTestId('nav-indicator')).toBeVisible();
  await expect.poll(error).toBeLessThan(2);
  await page.locator('nav a[href="#/memory"]').click();
  await expect.poll(error).toBeLessThan(2);
});

test('筛选条换行时高亮仅覆盖所选按钮', async ({ page }) => {
  await page.setViewportSize({width:850,height:900});
  await page.getByRole('button',{name:'done (1)',exact:true}).click();
  await expect.poll(() => page.locator('[data-active="true"]').evaluate(el => {
    const selected = el.getBoundingClientRect();
    const pill = el.parentElement.querySelector('[data-testid="task-status-indicator"]').getBoundingClientRect();
    return Math.abs(selected.top-pill.top)+Math.abs(selected.left-pill.left)+
      Math.abs(selected.height-pill.height)+Math.abs(selected.width-pill.width);
  })).toBeLessThan(2);
});
