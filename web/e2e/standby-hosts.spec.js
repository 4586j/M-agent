import {test,expect} from '@playwright/test';

async function prepare(page,personal){
  let polls=0;
  await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url()),path=url.pathname;
    if(path.startsWith('/agent/panel-assets/'))return route.continue();
    if(!path.startsWith('/agent/')&&!['/stats','/workers','/tasks','/budget','/sessions','/providers','/health'].includes(path))return route.continue();
    if(req.method()!=='GET')throw new Error(`Unexpected business write: ${path}`);
    polls++;
    let data={};
    if(path==='/agent/status')data={model:'fake',oneshot_usage:{},session_usage:{}};
    else if(path==='/agent/history')data={runs:[],total:0};
    else if(path==='/agent/sessions'||path==='/sessions')data={sessions:[]};
    else if(path==='/agent/tools')data={tools:[],skills:[],mcp_servers:[]};
    else if(path==='/stats')data={mode:'local',tasks:{},workers:{},cost:{}};
    else if(path==='/workers'||path==='/tasks')data=[];
    else if(path==='/providers')data={providers:{},default:''};
    await route.fulfill({json:data});
  });
  await page.goto(personal?'/panel':'/#/command');
  await expect(page.getByLabel('待机设置',{exact:true})).toBeVisible();
  return ()=>polls;
}
const dialog=page=>page.getByRole('dialog',{name:'呼吸花园待机画面',exact:true});
const restore=page=>page.getByRole('button',{name:'按住返回工作区，或按 Enter 确认',exact:true});
async function manual(page){const button=page.getByRole('button',{name:'立即待机',exact:true});if(!await button.isVisible())await page.getByLabel('待机设置',{exact:true}).click();await button.click();await expect(dialog(page)).toBeVisible();}

test('默认五分钟闲置自动遮挡，操作会重置计时，恢复后草稿和焦点仍在',async({page},info)=>{
  const personal=info.project.name==='personal';
  await page.clock.install();const polls=await prepare(page,personal);
  const input=personal?page.locator('#prompt'):page.getByPlaceholder('例如：分析 workspace/test.csv 并报告行数');
  await input.fill('未发送的项目草稿');
  await page.clock.fastForward(299000);await expect(dialog(page)).toBeHidden();
  await input.press('End');
  await page.clock.fastForward(299000);await expect(dialog(page)).toBeHidden();
  await page.clock.fastForward(1500);await expect(dialog(page)).toBeVisible();
  if(!personal){const before=polls();await page.clock.runFor(6000);expect(polls()).toBeGreaterThan(before);}
  await restore(page).press('Enter');await expect(dialog(page)).toBeHidden();
  await expect(input).toHaveValue('未发送的项目草稿');await expect(input).toBeFocused();
});

test('个人执行的流式结果在待机期间继续接收',async({page},info)=>{
  test.skip(info.project.name!=='personal','个人流式入口专项，编排后台轮询由自动待机用例覆盖');
  await prepare(page,true);
  let release,requested=false;
  const gate=new Promise(resolve=>release=resolve);
  await page.route('**/agent/run/stream',async route=>{requested=true;await gate;await route.fulfill({contentType:'text/event-stream',body:'event: text_delta\ndata: "待机期间的执行输出"\n\nevent: result\ndata: {"result":"后台执行已完成","num_turns":1}\n\n'});});
  try{
    await page.locator('#prompt').fill('只验证模拟流式接收');await page.locator('#btn-dispatch').click();await expect.poll(()=>requested).toBe(true);
    await manual(page);release();await expect(page.locator('body')).toContainText('待机期间的执行输出');
    await expect(dialog(page)).toBeVisible();await restore(page).press('Enter');await expect(page.locator('#btn-dispatch')).toBeEnabled();
  }finally{release();}
});

test('手动待机支持交互，鼠标短按、Escape、后台快捷键不揭开，新增 Portal 也被隔离',async({page},info)=>{
  await prepare(page,info.project.name==='personal');await manual(page);
  await page.mouse.move(850,350);await page.mouse.click(850,350);
  await page.keyboard.press('Escape');await restore(page).click();await page.keyboard.press('Control+k');await page.keyboard.press('r');
  await expect(dialog(page)).toBeVisible();
  await page.evaluate(()=>{const p=document.createElement('div');p.id='late-portal';p.textContent='不应出现在待机画面的项目数据';document.body.append(p);});
  await expect(page.locator('#late-portal')).toHaveAttribute('inert','');
  await expect(page.locator('#late-portal')).toHaveAttribute('aria-hidden','true');
  const trap=await restore(page).evaluate(el=>{el.focus();return true;});expect(trap).toBe(true);
  await page.keyboard.press('Tab');await expect(restore(page)).toBeFocused();
  await restore(page).press('Enter');await expect(dialog(page)).toBeHidden();
  await expect(page.locator('#late-portal')).not.toHaveAttribute('inert','');
});

test('待机刷新后保持遮挡，设置保存且不会跨标签自动解锁',async({page,context},info)=>{
  const personal=info.project.name==='personal',scope=info.project.name;
  await prepare(page,personal);
  await page.getByLabel('待机设置',{exact:true}).click();
  await page.getByLabel('闲置后待机',{exact:true}).selectOption('60');
  await page.getByLabel('花园表现方式',{exact:true}).selectOption('particles');
  await manual(page);
  await page.reload();await expect(dialog(page)).toBeVisible();await expect(dialog(page)).toContainText('星尘花园');
  const second=await context.newPage();await prepare(second,personal);await manual(second);
  await restore(page).press('Enter');await expect(dialog(page)).toBeHidden();
  await expect(dialog(second)).toBeVisible();await second.close();
  await page.getByLabel('待机设置',{exact:true}).click();await expect(page.getByLabel('闲置后待机',{exact:true})).toHaveValue('60');
  expect(await page.evaluate(scope=>JSON.parse(localStorage.getItem(`m-agent.standby.${scope}.settings`)).seconds,scope)).toBe(60);
});

test('关闭自动待机仍可手动进入，减少动态下画面静止，离开返回按钮取消长按',async({page},info)=>{
  await page.emulateMedia({reducedMotion:'reduce'});await page.clock.install();await prepare(page,info.project.name==='personal');
  await page.getByLabel('待机设置',{exact:true}).click();await page.getByLabel('闲置后待机',{exact:true}).selectOption('0');
  await page.clock.fastForward(1200000);await expect(dialog(page)).toBeHidden();
  await page.getByRole('button',{name:'立即待机',exact:true}).click();await expect(dialog(page)).toBeVisible();
  const canvas=dialog(page).locator('canvas');await page.clock.runFor(100);
  const before=await canvas.evaluate(el=>el.toDataURL());
  await page.mouse.click(850,350);await page.keyboard.press('r');await page.clock.runFor(350);
  expect(await canvas.evaluate((el,before)=>el.toDataURL()===before,before)).toBe(true);
  const b=await restore(page).boundingBox();await page.mouse.move(b.x+b.width/2,b.y+b.height/2);await page.mouse.down();
  await page.clock.runFor(350);await page.mouse.move(20,20);await page.clock.runFor(800);await page.mouse.up();await expect(dialog(page)).toBeVisible();
  await restore(page).press('Enter');await expect(dialog(page)).toBeHidden();
});

test('手机待机覆盖完整视口，长按恢复，生命周期清理后不会重复挂载',async({page},info)=>{
  await page.setViewportSize({width:390,height:844});await prepare(page,info.project.name==='personal');await manual(page);
  const b=await dialog(page).boundingBox();expect(b.width).toBe(390);expect(b.height).toBe(844);
  const button=await restore(page).boundingBox();await page.mouse.move(button.x+button.width/2,button.y+button.height/2);await page.mouse.down();
  await expect(dialog(page)).toBeHidden({timeout:2500});await page.mouse.up();
  expect(await page.locator('[data-agent-standby-overlay]').count()).toBe(1);
  expect(await page.locator('[data-agent-standby-controls]').count()).toBe(1);
});

test('后台标签计时器未执行时，返回可见状态也先检查超时',async({page},info)=>{
  await page.clock.install();await prepare(page,info.project.name==='personal');
  await page.getByLabel('待机设置',{exact:true}).click();await page.getByLabel('闲置后待机',{exact:true}).selectOption('60');
  const now=await page.evaluate(()=>Date.now());
  await page.clock.setSystemTime(now+61000);
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await expect(dialog(page)).toBeVisible();await expect(restore(page)).toBeFocused();
});

test('已待机的刷新在模块加载前先遮挡，加载后返回按钮自动获得焦点',async({page},info)=>{
  const personal=info.project.name==='personal';await prepare(page,personal);await manual(page);
  let release;const gate=new Promise(resolve=>release=resolve);
  await page.route(personal?'**/agent/panel-assets/standby.js*':'**/src/components/StandbyControl.tsx*',async route=>{await gate;await route.continue();});
  try{
    await page.reload({waitUntil:'commit'});
    await expect(page.locator('html')).toHaveAttribute('data-agent-standby-boot','true');
    expect(await page.locator('body').evaluate(el=>getComputedStyle(el).visibility)).toBe('hidden');
    release();await expect(dialog(page)).toBeVisible();await expect(restore(page)).toBeFocused();
    await expect(page.locator('html')).not.toHaveAttribute('data-agent-standby-boot','true');
  }finally{release();}
});
