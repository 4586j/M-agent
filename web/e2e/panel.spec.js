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

test.describe('终端回归', () => {
  test('资源连接挂起时输入可用，超时后保持纯文本输出', async ({ page }) => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('https://cdn.jsdelivr.net/**', async route => {
      await gate;
      await route.abort();
    });
    try {
      await page.locator('#vt-terminal').click();
      await expect(page.locator('#term-in')).toBeFocused();
      await expect(page.locator('#term-cwd')).not.toHaveText('未连接');
      await page.locator('#term-in').fill('echo LOADING_OK');
      await page.locator('#term-in').press('Enter');
      await expect(page.locator('#term-out')).toContainText(/\$ echo LOADING_OK\s+LOADING_OK/);
      await expect(page.locator('#term-out')).toContainText('降级纯文本输出', { timeout: 8000 });
      await expect(page.locator('#term-out')).toBeVisible();
      await expect(page.locator('#term-view')).not.toHaveClass(/xterm-on/);
    } finally {
      release();
    }
  });

  test('资源不可用时仍能执行命令、显示输出和清屏', async ({ page }) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.abort());
    await page.locator('#vt-terminal').click();
    await expect(page.locator('#term-in')).toBeFocused();
    await expect(page.locator('#term-cwd')).not.toHaveText('未连接');
    await expect(page.locator('#term-out')).toContainText('降级纯文本输出');
    await page.locator('#term-in').fill('echo FALLBACK_OK');
    await page.locator('#term-in').press('Enter');
    await expect.poll(() => page.evaluate(() => termBusy)).toBe(false);
    // 既验证命令回显，也验证来自真实 WebSocket 后端的结果。
    await expect(page.locator('#term-out')).toContainText(/\$ echo FALLBACK_OK\s+FALLBACK_OK/);
    await expect(page.locator('#term-out')).toBeVisible();
    await page.getByRole('button', { name: '清屏', exact: true }).click();
    await expect(page.locator('#term-out')).toBeEmpty();
  });

  test('延迟加载保留已有输出，重复切换只初始化一次，清屏作用于当前渲染器', async ({ page }) => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('https://cdn.jsdelivr.net/**', async route => {
      await gate;
      const url = route.request().url();
      if(url.endsWith('.css')){
        await route.fulfill({ contentType: 'text/css', body: '.xterm-test { white-space: pre; }' });
      } else if(url.includes('addon-fit')){
        await route.fulfill({ contentType: 'application/javascript',
          body: 'window.FitAddon = { FitAddon: class { fit(){} } };' });
      } else {
        await route.fulfill({ contentType: 'application/javascript', body: `
          window.Terminal = class {
            constructor(){ window.terminalCreates = (window.terminalCreates || 0) + 1; }
            loadAddon(){}
            open(el){ this.el = document.createElement('pre'); this.el.className = 'xterm-test'; el.append(this.el); }
            write(text){ this.el.textContent += text.replace(/\\x1b\\[[0-9;]*m/g, ''); }
            reset(){ this.el.textContent = ''; }
            dispose(){ this.el?.remove(); }
          };
        ` });
      }
    });
    await page.locator('#vt-terminal').click();
    await expect(page.locator('#term-in')).toBeFocused();
    await expect(page.locator('#term-cwd')).not.toHaveText('未连接');
    await page.locator('#term-in').fill('echo EARLY_OUTPUT_OK');
    await page.locator('#term-in').press('Enter');
    await expect(page.locator('#term-out')).toContainText(/\$ echo EARLY_OUTPUT_OK\s+EARLY_OUTPUT_OK/);
    await page.locator('#vt-stream').click();
    await page.locator('#vt-terminal').click();
    release();
    await expect(page.locator('#term-view')).toHaveClass(/xterm-on/);
    await expect(page.locator('.xterm-test')).toContainText('EARLY_OUTPUT_OK');
    await expect(page.locator('.xterm-test')).toContainText('[shell:');
    await expect(page.locator('.xterm-test')).toHaveCSS('white-space', 'pre');
    expect(await page.evaluate(() => window.terminalCreates)).toBe(1);
    await page.getByRole('button', { name: '清屏', exact: true }).click();
    await expect(page.locator('.xterm-test')).toBeEmpty();
    await page.locator('#term-in').fill('echo AFTER_CLEAR_OK');
    await page.locator('#term-in').press('Enter');
    await expect(page.locator('.xterm-test')).toContainText(/\$ echo AFTER_CLEAR_OK\s+AFTER_CLEAR_OK/);
  });
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

test.describe('输入框命令', () => {
  test('用量和帮助等控制命令不派发模型请求，未知命令不误发送', async ({ page }) => {
    const executions = [];
    await page.route('**/agent/run/**', route => { executions.push(route.request().url()); return route.abort(); });
    await page.route('**/agent/status', route => route.fulfill({json:{
      oneshot_usage:{runs:3, errors:1, total_turns:5, total_cost_usd:0.0123},
      session_usage:{sessions:0, total_cost_usd:0},
    }}));
    for(const command of ['/usage','/cost','/help','/','/plan','/unknown']){
      await page.locator('#prompt').fill(command);
      await page.locator('#prompt').press('Control+Enter');
      await expect(page.locator('#modal-mask')).toBeVisible();
      if(command === '/usage' || command === '/cost'){
        await expect(page.locator('.modal-body')).toContainText('$0.012300');
        await expect(page.locator('.modal-body')).toContainText('服务进程启动后');
      }
      await page.getByRole('button',{name:'关闭',exact:true}).click();
    }
    expect(executions).toEqual([]);
  });

  test('计划命令去掉前缀并携带会话，只对本次生效，失败终态可收尾', async ({ page }) => {
    const requests = [];
    await page.route('**/agent/run/async', async route => {
      requests.push(route.request().postDataJSON());
      await route.fulfill({status:202,json:{run_id:'slash-test',status:'running'}});
    });
    await page.route('**/agent/run/slash-test/events', route => route.fulfill({json:[]}));
    await page.route('**/agent/run/slash-test', route => route.fulfill({json:{status:'failed',error:{message:'计划测试失败'}}}));
    await page.evaluate(() => { sessionId='012345abcdef'; });
    await page.locator('#prompt').fill('/plan 分析项目\n列出步骤');
    await page.locator('#btn-dispatch').click();
    await expect(page.locator('#stream')).toContainText('计划测试失败');
    await expect(page.locator('#btn-dispatch')).toBeEnabled();
    expect(requests).toEqual([expect.objectContaining({prompt:'分析项目\n列出步骤',
      planning_enabled:true,session_id:'012345abcdef'})]);
    await expect(page.locator('#plan-cb')).not.toBeChecked();
  });

  test('模型命令、提示补全及新会话可用，不携带旧会话', async ({ page }) => {
    await page.locator('#prompt').fill('/pl');
    await expect(page.locator('#slash-hints')).toBeVisible();
    await page.locator('#slash-hints button').click();
    await expect(page.locator('#prompt')).toHaveValue('/plan ');
    await page.locator('#prompt').fill('/model ark-code-latest');
    await page.locator('#prompt').press('Control+Enter');
    await expect(page.locator('#model')).toHaveValue('ark-code-latest');
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.route('**/agent/session/persist', route => route.fulfill({json:{session_id:'abcdef012345'}}));
    await page.route('**/agent/persisted-sessions', route => route.fulfill({json:{sessions:[{session_id:'abcdef012345',turn_count:0}]}}));
    await page.route('**/agent/persisted-sessions/abcdef012345', route => route.fulfill({json:{turns:[]}}));
    await page.evaluate(() => { sessionId='012345abcdef'; });
    await page.locator('#prompt').fill('/new');
    await page.locator('#prompt').press('Control+Enter');
    await expect(page.locator('#session-sel')).toHaveValue('abcdef012345');
    await expect(page.locator('#prompt')).toBeEmpty();
    await expect(page.locator('#stream')).toContainText('会话已就绪');
  });

  test('双斜杠可发送原文，停止命令发出取消请求', async ({ page }) => {
    await page.evaluate(() => { live=true; currentRunId='cancel-test'; });
    const cancelled = page.waitForRequest(r => r.url().includes('/agent/run/cancel-test') && r.method()==='DELETE');
    await page.route('**/agent/run/cancel-test', route => route.fulfill({json:{cancel_requested:true}}));
    await page.locator('#prompt').fill('/stop');
    await page.locator('#btn-dispatch').click();
    await cancelled;
    await expect(page.locator('#stream')).toContainText('已请求中断');
    const submitted = page.waitForRequest('**/agent/run/stream');
    await page.route('**/agent/run/stream', route => route.fulfill({contentType:'text/event-stream',
      body:'event: result\ndata: {"result":"已完成","num_turns":1}\n\n'}));
    await page.locator('#prompt').fill('//tmp/example.txt 请分析');
    await page.locator('#prompt').press('Control+Enter');
    expect((await submitted).postDataJSON().prompt).toBe('/tmp/example.txt 请分析');
  });
});

test.describe('会话删除', () => {
  const sid = '012345abcdef';
  test.beforeEach(async ({ page }) => {
    await page.route('**/agent/persisted-sessions', route => route.fulfill({json:{sessions:[
      {session_id:sid,title:'无用的会话',turn_count:2},
      {session_id:'abcdef012345',title:'保留的会话',turn_count:1},
    ]}}));
    await page.route(`**/agent/persisted-sessions/${sid}`, route => route.fulfill({json:{turns:[]}}));
    await page.evaluate(() => loadSessions());
    await expect(page.locator('#session-delete')).toBeDisabled();
    await page.locator('#session-sel').selectOption(sid);
    await expect(page.locator('#stream')).toContainText('会话已就绪');
    await expect(page.locator('#session-delete')).toBeEnabled();
  });

  test('取消不删除，确认后清除会话选择并保留其他会话', async ({ page }) => {
    const deleted = [];
    await page.route(`**/agent/persisted-sessions/${sid}`, async route => {
      if(route.request().method()==='DELETE') deleted.push(sid);
      await route.fulfill({json:{status:'deleted'}});
    });
    await page.locator('#session-delete').click();
    await expect(page.locator('.modal-body')).toContainText('无用的会话');
    await page.getByRole('button',{name:'取消',exact:true}).click();
    expect(deleted).toEqual([]);
    await expect(page.locator('#session-sel')).toHaveValue(sid);
    await page.route('**/agent/persisted-sessions', route => route.fulfill({json:{sessions:[
      {session_id:'abcdef012345',title:'保留的会话',turn_count:1},
    ]}}));
    await page.locator('#session-delete').click();
    await page.getByRole('button',{name:'确认删除',exact:true}).click();
    await expect(page.locator('#stream')).toContainText('会话已删除');
    await expect(page.locator('#session-sel')).toHaveValue('');
    await expect(page.locator('#session-sel')).toContainText('保留的会话');
    await expect(page.locator('#session-sel')).not.toContainText('无用的会话');
    await expect(page.locator('#session-delete')).toBeDisabled();
    expect(await page.evaluate(() => localStorage.getItem('agent_panel_sid'))).toBeNull();
    expect(deleted).toEqual([sid]);
  });

  test('斜杠删除同样需要确认，接口失败保留会话', async ({ page }) => {
    await page.route(`**/agent/persisted-sessions/${sid}`, route => route.fulfill({status:500,json:{detail:'保存目录不可写'}}));
    await page.locator('#prompt').fill('/delete');
    await page.locator('#prompt').press('Control+Enter');
    await expect(page.locator('.modal-title')).toHaveText('删除会话？');
    await page.getByRole('button',{name:'确认删除',exact:true}).click();
    await expect(page.locator('.modal-title')).toHaveText('删除失败');
    await expect(page.locator('.modal-body')).toContainText('保存目录不可写');
    await expect(page.locator('#session-sel')).toHaveValue(sid);
    await expect(page.locator('#session-delete')).toBeEnabled();
    expect(await page.evaluate(() => localStorage.getItem('agent_panel_sid'))).toBe(sid);
  });

  test('执行中禁止删除和切换会话', async ({ page }) => {
    await page.evaluate(() => { live=true; updateSessionControls(); });
    await expect(page.locator('#session-delete')).toBeDisabled();
    await expect(page.locator('#session-sel')).toBeDisabled();
    await expect(page.locator('#session-new')).toBeDisabled();
  });
});

test('会话重命名保留引号并发送 JSON，用量包括标题提炼', async ({ page, request }) => {
  const sid = (await (await request.post('/agent/session/persist')).json()).session_id;
  const quotedTitle = '审查 "登录" 问题';
  await request.patch(`/agent/persisted-sessions/${sid}`, {data:{title:quotedTitle}});
  await page.evaluate(() => loadSessions());
  await page.locator('#session-sel').selectOption(sid);
  await expect(page.locator('#session-rename')).toBeEnabled();
  await page.locator('#session-rename').click();
  await expect(page.locator('#rename-title-in')).toHaveValue(quotedTitle);
  await page.locator('#rename-title-in').fill('已修复 "登录" 问题');
  const response = page.waitForResponse(r=>r.url().includes(`/agent/persisted-sessions/${sid}`) && r.request().method()==='PATCH');
  await page.getByRole('button',{name:'保存',exact:true}).click();
  const saved = await response;
  expect(saved.status()).toBe(200);
  expect(saved.request().postDataJSON()).toEqual({title:'已修复 "登录" 问题'});
  await expect(page.locator('#session-sel')).toContainText('已修复 "登录" 问题');
  await page.route('**/agent/status', route=>route.fulfill({json:{title_usage:{calls:1,
    errors:0,total_cost_usd:0.123,unknown_cost_calls:0,usage:{input_tokens:25,output_tokens:5}}}}));
  await page.locator('#prompt').fill('/usage');
  await page.locator('#prompt').press('Control+Enter');
  await expect(page.locator('.modal-body')).toContainText('标题提炼：1 次');
  await expect(page.locator('.modal-body')).toContainText('$0.123000');
  await expect(page.locator('.modal-body')).toContainText('输入 25，输出 5');
  await request.delete(`/agent/persisted-sessions/${sid}`);
});
