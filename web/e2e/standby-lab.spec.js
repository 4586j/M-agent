import {test, expect} from '@playwright/test';

// Observe actual large rings drawn on a chosen canvas, without exposing application state.
async function observeRings(page, id) {
  await page.evaluate(id => {
    window.gardenRings=[];
    const ctx=document.getElementById(id).getContext('2d'),arc=ctx.arc;
    ctx.arc=function(x,y,r,...args){if(r>20)window.gardenRings.push({x,y,r});return arc.call(this,x,y,r,...args);};
  },id);
}

test('五套动画可切换，设置和选择偏好可用，无外部资源或脚本异常', async ({page}) => {
  const errors = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (!request.url().startsWith('http://127.0.0.1:7079/')) external.push(request.url());});
  await page.goto('/standby-lab/');
  for (const [name, id] of [['引力编队','orbit'],['极光织幕','aurora'],['水墨远山','ink'],['呼吸花园','bloom'],['折光矩阵','matrix']]) {
    await page.getByRole('button', {name: new RegExp('^' + name + '，')}).click();
    await expect(page.locator('#stage')).toHaveAttribute('data-theme', id);
    await expect(page.locator('#sceneName')).toHaveText(name);
    await page.waitForTimeout(80);
  }
  await page.locator('#clockToggle').uncheck();
  await expect(page.locator('body')).toHaveClass(/clock-hidden/);
  await page.locator('#brightnessRange').fill('50');
  await expect(page.locator('#brightnessValue')).toHaveText('50%');
  await page.getByRole('button', {name: '记下这个方案', exact: true}).click();
  await expect(page.locator('#preferenceTitle')).toContainText('折光矩阵');
  expect(await page.evaluate(() => localStorage.getItem('m-agent.standby-lab.choice'))).toBe('matrix');
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});

test('遮挡后鼠标移动、短按和 Escape 不恢复；Enter 明确恢复并归还焦点', async ({page}) => {
  await page.goto('/standby-lab/');
  await page.getByRole('button', {name: '全屏体验'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.locator('#labApp').evaluate(el => el.inert)).toBe(true);
  await page.mouse.move(10, 10);
  await page.keyboard.press('Escape');
  const restore = page.locator('#returnButton');
  await restore.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#standbyLayer')).toHaveAttribute('data-theme', 'aurora');
  await restore.press('Enter');
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.locator('#experienceButton')).toBeFocused();
  expect(await page.locator('#labApp').evaluate(el => el.inert)).toBe(false);
});

test('模拟闲置自动进入，长按可恢复，减少动态呈现静态画面', async ({page}) => {
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.goto('/standby-lab/');
  await expect(page.locator('#reducedToggle')).toBeChecked();
  await page.waitForTimeout(150);
  const before = await page.locator('#stageCanvas').evaluate(el => el.toDataURL());
  await page.waitForTimeout(250);
  expect(await page.locator('#stageCanvas').evaluate(el => el.toDataURL())).toBe(before);
  await page.getByRole('button', {name: '模拟离开 5 秒'}).click();
  await expect(page.getByRole('dialog')).toBeVisible({timeout:7500});
  const restore = page.locator('#returnButton');
  const box = await restore.boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
  await page.mouse.down();
  await expect(page.getByRole('dialog')).toBeHidden({timeout:2000});
  await page.mouse.up();
});

test('移动端无页面横向溢出，横向方案列表仍可选', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto('/standby-lab/');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);
  await page.getByRole('button', {name: /^折光矩阵，/}).click();
  await expect(page.locator('#sceneName')).toHaveText('折光矩阵');
});

test('真实闲置计时会被操作重置，到期才遮挡', async ({page}) => {
  await page.clock.install();
  await page.goto('/standby-lab/');
  await page.locator('#idleSelect').selectOption('15');
  await page.clock.fastForward(14000);
  await expect(page.getByRole('dialog')).toBeHidden();
  await page.mouse.move(110,120);
  await page.clock.fastForward(14000);
  await expect(page.getByRole('dialog')).toBeHidden();
  await page.clock.fastForward(1500);
  await expect(page.getByRole('dialog')).toBeVisible();
});

test('呼吸花园可比较流线与粒子，减少动态时切换仍可重绘并保留选择', async ({page}) => {
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.goto('/standby-lab/?scene=bloom&bloom=lines');
  await expect(page.locator('#bloomVariants')).toBeVisible();
  await expect(page.getByRole('button',{name:'流线花冠',exact:true})).toHaveAttribute('aria-pressed','true');
  await page.waitForTimeout(100);
  const lines=await page.locator('#stageCanvas').evaluate(el=>el.toDataURL());
  await page.getByRole('button',{name:'星尘花园',exact:true}).click();
  await expect(page.locator('#sceneDescription')).toContainText('细小的星尘');
  const particles=await page.locator('#stageCanvas').evaluate(el=>el.toDataURL());
  expect(lines!==particles).toBe(true);
  await page.getByRole('button',{name:'记下这个方案',exact:true}).click();
  await expect(page.locator('#preferenceTitle')).toContainText('星尘花园');
  await page.goto('/standby-lab/?scene=bloom');
  await expect(page.getByRole('button',{name:'星尘花园',exact:true})).toHaveAttribute('aria-pressed','true');
});

test('花园点击产生扩散涟漪并消退，全屏键盘和点击交互不会揭开遮挡', async ({page}) => {
  await page.clock.install();
  await page.goto('/standby-lab/?scene=bloom&bloom=lines');
  await page.clock.runFor(150);
  await observeRings(page,'stageCanvas');
  const stage=await page.locator('#stage').boundingBox();
  await page.mouse.click(stage.x+stage.width*.67,stage.y+stage.height*.5);
  await page.clock.runFor(450);
  expect(await page.evaluate(()=>window.gardenRings.some(ring=>ring.r>40))).toBe(true);
  await page.clock.runFor(2200);
  await page.evaluate(()=>window.gardenRings=[]);
  await page.clock.runFor(150);
  expect(await page.evaluate(()=>window.gardenRings.length)).toBe(0);
  await page.getByRole('button',{name:'星尘花园',exact:true}).click();
  await page.getByRole('button',{name:'唤起涟漪',exact:true}).click();
  await page.clock.runFor(450);
  expect(await page.evaluate(()=>window.gardenRings.length)).toBeGreaterThan(0);
  await page.getByRole('button',{name:'全屏体验'}).click();
  await observeRings(page,'fullCanvas');
  await page.keyboard.press('r');
  await page.clock.runFor(450);
  expect(await page.evaluate(()=>window.gardenRings.length)).toBeGreaterThan(0);
  await page.mouse.click(650,250);
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.locator('#labApp').evaluate(el=>el.inert)).toBe(true);
});

test('暂停和减少动态同时停止花园指针与涟漪效果', async ({page}) => {
  await page.goto('/standby-lab/?scene=bloom');
  await page.locator('#pauseButton').click();
  for(const reduce of [false,true]) {
    if(reduce){await page.locator('#pauseButton').click();await page.locator('#reducedToggle').check();}
    await expect(page.locator('#bloomRippleButton')).toBeDisabled();
    await page.waitForTimeout(100);
    const before=await page.locator('#stageCanvas').evaluate(el=>el.toDataURL());
    const box=await page.locator('#stage').boundingBox();
    await page.mouse.move(box.x+box.width*.65,box.y+box.height*.45);
    await page.mouse.click(box.x+box.width*.7,box.y+box.height*.55);
    await page.waitForTimeout(150);
    expect(await page.locator('#stageCanvas').evaluate((el,before)=>el.toDataURL()===before,before)).toBe(true);
  }
});

test('触屏轻点花园可产生涟漪，保留页面滚动', async ({browser}) => {
  const context=await browser.newContext({hasTouch:true,viewport:{width:390,height:844}});
  try{
    const page=await context.newPage();
    await page.goto('http://127.0.0.1:7079/standby-lab/?scene=bloom&bloom=particles');
    await observeRings(page,'stageCanvas');
    const box=await page.locator('#stage').boundingBox();
    await page.touchscreen.tap(box.x+box.width*.65,box.y+box.height*.5);
    await expect.poll(()=>page.evaluate(()=>window.gardenRings.length)).toBeGreaterThan(0);
    expect(await page.locator('#stage').evaluate(el=>getComputedStyle(el).touchAction)).toBe('auto');
  }finally{await context.close();}
});

test('主画面按约 60 帧刷新，缩略图独立降频，全屏不重绘底层画布', async ({page}) => {
  await page.clock.install();
  await page.goto('/standby-lab/?scene=bloom&bloom=lines');
  await page.clock.runFor(150);
  await page.evaluate(()=>{
    window.paintCounts={stage:0,full:0,thumbs:0};
    const transform=CanvasRenderingContext2D.prototype.setTransform;
    CanvasRenderingContext2D.prototype.setTransform=function(...args){
      if(this.canvas.id==='stageCanvas')window.paintCounts.stage++;
      else if(this.canvas.id==='fullCanvas')window.paintCounts.full++;
      else window.paintCounts.thumbs++;
      return transform.apply(this,args);
    };
  });
  await page.clock.runFor(1000);
  let counts=await page.evaluate(()=>window.paintCounts);
  expect(counts.stage).toBeGreaterThanOrEqual(55);
  expect(counts.stage).toBeLessThanOrEqual(65);
  expect(counts.thumbs).toBeGreaterThan(0);
  expect(counts.thumbs).toBeLessThanOrEqual(70);
  expect(counts.full).toBe(0);
  await page.locator('#experienceButton').click();
  await page.clock.runFor(100);
  await page.evaluate(()=>window.paintCounts={stage:0,full:0,thumbs:0});
  await page.clock.runFor(1000);
  counts=await page.evaluate(()=>window.paintCounts);
  expect(counts.full).toBeGreaterThanOrEqual(55);
  expect(counts.full).toBeLessThanOrEqual(65);
  expect(counts.stage).toBe(0);
  expect(counts.thumbs).toBe(0);
});
