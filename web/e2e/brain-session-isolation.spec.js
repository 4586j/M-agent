import { test, expect } from '@playwright/test';

test('切换 Brain 不携带旧会话，返回原 Brain 恢复选择，地址改变后不复用', async ({page}) => {
  const dispatched = [];
  await page.addInitScript(() => {
    localStorage.setItem('orch_connections', JSON.stringify([
      {id:'a',name:'Brain A',baseUrl:'http://brain-a.test',apiKey:''},
      {id:'b',name:'Brain B',baseUrl:'http://brain-b.test',apiKey:''},
    ]));
    localStorage.setItem('orch_active_connection','a');
    localStorage.setItem('orch_dispatch_session_id','session-from-a');
  });
  await page.route('http://brain-*.test/**', async route => {
    const u = new URL(route.request().url());
    const sid = u.host === 'brain-a.test' ? 'session-from-a' : 'session-from-b';
    let data = {};
    if(u.pathname === '/understanding/policy') data = {required:false};
    if(u.pathname === '/sessions') data = {sessions:[{session_id:sid,title:sid,turn_count:1}]};
    else if(u.pathname === '/workers' || u.pathname === '/tasks') data = [];
    else if(u.pathname === '/providers') data = {providers:{},default:''};
    else if(u.pathname === '/stats') data = {mode:'local',tasks:{},workers:{},cost:{}};
    else if(u.pathname === '/dispatch') {
      dispatched.push({brain:u.host,body:route.request().postDataJSON()});
      data = {task_id:'probe',status:'done',output:'probe only',artifacts:[]};
    }
    await route.fulfill({json:data});
  });
  await page.goto('/#/command');
  const selector = page.locator('select[title^="选中会话"]');
  await expect(selector).toHaveValue('session-from-a');
  await page.evaluate(async () => { const m=await import('/src/api/connections.ts'); m.activate('b'); });
  await expect(selector).toContainText('session-from-b');
  await expect(selector).toHaveValue('');
  await page.getByPlaceholder('例如：分析 workspace/test.csv 并报告行数').fill('验证 B 的独立派发');
  await page.getByRole('button',{name:'派发',exact:true}).click();
  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0].brain).toBe('brain-b.test');
  expect(dispatched[0].body.session_id).toBeNull();
  await selector.selectOption('session-from-b');
  await page.evaluate(async () => { const m=await import('/src/api/connections.ts'); m.activate('a'); });
  await expect(selector).toHaveValue('session-from-a');
  await page.evaluate(async () => { const m=await import('/src/api/connections.ts'); m.activate('b'); });
  await expect(selector).toHaveValue('session-from-b');
  await page.evaluate(async () => {
    const m=await import('/src/api/connections.ts');
    m.saveConnection({...m.getActiveConnection(),baseUrl:'http://brain-c.test'});
  });
  await expect(selector).toHaveValue('');
});

test('旧 Brain 的会话创建响应迟到，不选入新 Brain', async ({page}) => {
  let release;
  const gate = new Promise(resolve => { release=resolve; });
  await page.addInitScript(() => {
    localStorage.setItem('orch_connections',JSON.stringify([
      {id:'a',name:'A',baseUrl:'http://brain-a.test',apiKey:''},
      {id:'b',name:'B',baseUrl:'http://brain-b.test',apiKey:''},
    ]));
    localStorage.setItem('orch_active_connection','a');
  });
  await page.route('http://brain-*.test/**', async route => {
    const u=new URL(route.request().url());
    if(u.pathname==='/sessions' && route.request().method()==='POST') {
      await gate; return route.fulfill({json:{session_id:'late-session-a'}});
    }
    let data={};
    if(u.pathname === '/understanding/policy') data={required:false};
    if(u.pathname==='/sessions') data={sessions:[]};
    else if(u.pathname==='/tasks'||u.pathname==='/workers') data=[];
    else if(u.pathname==='/providers') data={providers:{},default:''};
    else if(u.pathname==='/stats') data={mode:'local',tasks:{},workers:{},cost:{}};
    await route.fulfill({json:data});
  });
  await page.goto('/#/command');
  const created=page.waitForRequest(r=>r.url().endsWith('/sessions')&&r.method()==='POST');
  await page.getByRole('button',{name:'＋',exact:true}).click();
  await created;
  await page.evaluate(async()=>{const m=await import('/src/api/connections.ts');m.activate('b');});
  const completed = page.waitForResponse(r => r.url().endsWith('/sessions') && r.request().method() === 'POST');
  release();
  await (await completed).finished();
  await expect(page.locator('select[title^="选中会话"]')).toHaveValue('');
  await expect.poll(()=>page.evaluate(()=>Object.entries(localStorage).some(([k,v])=>k.startsWith('orch_dispatch_session_id:')&&v==='late-session-a'))).toBe(false);
});

test('节点跳转的派发目标和草稿不跨 Brain 保留', async ({page}) => {
  const dispatched = [];
  await page.addInitScript(() => {
    localStorage.setItem('orch_connections', JSON.stringify([
      {id:'a',name:'A',baseUrl:'http://brain-a.test',apiKey:''},
      {id:'b',name:'B',baseUrl:'http://brain-b.test',apiKey:''},
    ]));
    localStorage.setItem('orch_active_connection','a');
  });
  await page.route('http://brain-*.test/**', async route => {
    const u = new URL(route.request().url());
    let data = {};
    if(u.pathname === '/understanding/policy') data = {required:false};
    if (u.pathname === '/workers') data = [{worker_id:'same-worker-id',role:'executor',host:'127.0.0.1',port:8001,load:0,capabilities:{},last_heartbeat:0}];
    else if (u.pathname === '/tasks') data = [];
    else if (u.pathname === '/sessions') data = {sessions:[]};
    else if (u.pathname === '/providers') data = {providers:{},default:''};
    else if (u.pathname === '/stats') data = {mode:'local',tasks:{},workers:{},cost:{}};
    else if (u.pathname === '/dispatch') {
      dispatched.push(route.request().postDataJSON());
      data = {task_id:'probe',status:'done',output:'probe only',artifacts:[]};
    }
    await route.fulfill({json:data});
  });
  await page.goto('/#/nodes');
  await page.getByRole('button', {name:'派发到此节点',exact:true}).click();
  const target = page.locator('select[title^="空 = 自动按 role"]');
  await expect(target).toHaveValue('same-worker-id');
  const prompt = page.getByPlaceholder('例如：分析 workspace/test.csv 并报告行数');
  await prompt.fill('A 的未发送草稿');
  await page.evaluate(async () => {const m=await import('/src/api/connections.ts');m.activate('b');});
  await expect(target).toHaveValue('');
  await expect(prompt).toHaveValue('');
  await prompt.fill('B 的独立任务');
  await page.getByRole('button', {name:'派发',exact:true}).click();
  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0].worker_id).toBeNull();
});
