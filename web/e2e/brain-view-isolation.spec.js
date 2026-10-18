import { test, expect } from '@playwright/test';

let writes;
test.beforeEach(async ({page}) => {
  writes = [];
  await page.addInitScript(() => {
    localStorage.setItem('orch_connections', JSON.stringify([
      {id:'a',name:'A',baseUrl:'http://brain-a.test',apiKey:''},
      {id:'b',name:'B',baseUrl:'http://brain-b.test',apiKey:''},
    ]));
    localStorage.setItem('orch_active_connection','a');
  });
  await page.route('http://brain-*.test/**', async route => {
    const request = route.request();
    const u = new URL(request.url());
    const task = {task_id:'same-task',role:'executor',prompt:'隔离验证任务',status:'done',
      output:u.host,artifacts:[],created_at:'1',updated_at:'1',worker_id:null};
    if (request.method() !== 'GET') writes.push({brain:u.host,path:u.pathname,method:request.method(),body:request.postDataJSON()});
    let data = {};
    if (u.pathname === '/stats') data = {mode:'local',tasks:{done:1},workers:{},cost:{}};
    else if (u.pathname === '/memory/core') data = {memories:[{name:'shared',content:u.host}]};
    else if (u.pathname === '/providers') data = {providers:{},default:''};
    else if (u.pathname === '/schedules') data = [];
    else if (u.pathname === '/workers') data = [{worker_id:'same-worker',role:'executor',host:'127.0.0.1',port:8001,capabilities:{},load:0,last_heartbeat:0}];
    else if (u.pathname === '/proxy/node/status') data = {worker_id:'same-worker',workspace:{path:`D:/${u.host}`,name:u.host,exists:true},skills:[],mcp_servers:[],runs:{running:0,total:0}};
    else if (u.pathname === '/tasks') data = [task];
    else if (u.pathname.startsWith('/tasks/')) data = task;
    else if (u.pathname === '/budgets') data = {global_limit_usd:null,daily_limit_usd:null,spent_total_usd:0,spent_today_usd:0};
    else if (u.pathname === '/knowledge') data = {docs:[{doc_id:'doc-1',title:'测试文档',chunk_count:1,content_chars:10,updated_at:1}]};
    await route.fulfill({json:data});
  });
});

const switchBrain = page => page.evaluate(async () => {
  const m = await import('/src/api/connections.ts'); m.activate('b');
});

test('核心记忆旧草稿不跨 Brain 保存，修改名称保留当前草稿', async ({page}) => {
  await page.goto('/#/memory');
  await page.getByRole('button',{name:'编辑',exact:true}).click();
  await page.locator('textarea').fill('A 的草稿');
  await page.evaluate(async () => {
    const m = await import('/src/api/connections.ts');
    m.saveConnection({...m.getActiveConnection(),name:'A renamed'});
  });
  await expect(page.locator('textarea')).toHaveValue('A 的草稿');
  await switchBrain(page);
  await expect(page.locator('textarea')).toHaveCount(0);
  await expect(page.getByText('brain-b.test',{exact:true}).first()).toBeVisible();
  await page.getByRole('button',{name:'编辑',exact:true}).click();
  await expect(page.locator('textarea')).toHaveValue('brain-b.test');
  await page.locator('textarea').fill('B 的修改');
  await page.getByRole('button',{name:'保存',exact:true}).click();
  await expect.poll(() => writes.filter(w => w.path === '/memory/core/shared').length).toBe(1);
  expect(writes.find(w => w.path === '/memory/core/shared')).toMatchObject({brain:'brain-b.test',body:{content:'B 的修改'}});
});

for (const scenario of [
  {route:'providers',button:/新增 Provider/,field:'名称（如 gw-main）'},
  {route:'schedules',button:/新建调度/,field:'名称（如 日报生成）'},
]) {
  test(`${scenario.route} 创建表单在切换后关闭并清空草稿`, async ({page}) => {
    await page.goto(`/#/${scenario.route}`);
    await page.getByRole('button',{name:scenario.button}).click();
    await page.getByPlaceholder(scenario.field,{exact:true}).fill('A draft');
    await switchBrain(page);
    await expect(page.getByPlaceholder(scenario.field,{exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:scenario.button}).click();
    await expect(page.getByPlaceholder(scenario.field,{exact:true})).toHaveValue('');
    expect(writes).toHaveLength(0);
  });
}

test('节点工作区编辑在切换后关闭，不继承同名节点的路径', async ({page}) => {
  await page.goto('/#/nodes');
  await page.getByRole('button',{name:'详情',exact:true}).click();
  const field = page.getByPlaceholder('绝对路径（目录须存在）');
  await expect(field).toHaveValue('D:/brain-a.test');
  await field.fill('D:/A-draft');
  await switchBrain(page);
  await expect(field).toHaveCount(0);
  await page.getByRole('button',{name:'详情',exact:true}).click();
  await expect(field).toHaveValue('D:/brain-b.test');
  expect(writes).toHaveLength(0);
});

test('同一连接改地址时关闭任务详情，立即加载新 Brain 的任务', async ({page}) => {
  await page.goto('/#/tasks');
  await page.getByRole('button',{name:/same-task/}).click();
  await expect(page.getByRole('dialog',{name:'任务详情'})).toBeVisible();
  await page.evaluate(async () => {
    const m = await import('/src/api/connections.ts');
    m.saveConnection({...m.getActiveConnection(),baseUrl:'http://brain-b.test'});
  });
  await expect(page.getByRole('dialog',{name:'任务详情'})).toHaveCount(0);
  await page.getByRole('button',{name:/same-task/}).click();
  await expect(page.getByRole('dialog',{name:'任务详情'})).toContainText('brain-b.test');
});

test('同一连接更换密钥时清空编辑状态，密钥不出现在 DOM 属性中', async ({page}) => {
  await page.goto('/#/memory');
  await page.getByRole('button',{name:'编辑',exact:true}).click();
  await page.locator('textarea').fill('旧凭据下的草稿');
  await page.evaluate(async () => {
    const m = await import('/src/api/connections.ts');
    m.saveConnection({...m.getActiveConnection(),apiKey:'test-only-rotated-key'});
  });
  await expect(page.locator('textarea')).toHaveCount(0);
  expect(await page.locator('main').innerHTML()).not.toContain('test-only-rotated-key');
});

test('知识文档删除只确认一次，拒绝时不发删除请求', async ({page}) => {
  await page.goto('/#/memory');
  await page.getByRole('button',{name:'知识库',exact:true}).click();
  await expect(page.getByText('测试文档',{exact:true})).toBeVisible();
  let confirmations = 0;
  let accept = false;
  page.on('dialog', async dialog => { confirmations++; if (accept) await dialog.accept(); else await dialog.dismiss(); });
  await page.getByRole('button',{name:'删除',exact:true}).click();
  expect(confirmations).toBe(1);
  expect(writes).toHaveLength(0);
  accept = true;
  await page.getByRole('button',{name:'删除',exact:true}).click();
  await expect.poll(() => writes.filter(w => w.method === 'DELETE').length).toBe(1);
  expect(confirmations).toBe(2);
});
