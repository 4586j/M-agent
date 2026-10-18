import {test,expect} from '@playwright/test';

test('多 Agent 编排台不安装个人理解核验，也不把核验数据送入派发',async({page})=>{
  const calls=[];
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.host!=='127.0.0.1:8126') return route.continue();
    const path=url.pathname;
    if(path.startsWith('/src/')||path.startsWith('/node_modules/')||path.startsWith('/@')||path==='/'||path.endsWith('.css')) return route.continue();
    if(path.includes('understanding')) calls.push(path);
    let data={};
    if(['/workers','/tasks'].includes(path))data=[];
    else if(path==='/sessions')data={sessions:[]};
    else if(path==='/providers')data={providers:{},default:''};
    else if(path==='/stats')data={mode:'local',workers:{},tasks:{},cost:{}};
    else if(path==='/dispatch'){calls.push(route.request().postDataJSON());data={task_id:'normal',status:'done',output:'ok'};}
    await route.fulfill({json:data});
  });
  await page.goto('/#/command');
  await page.getByPlaceholder('例如：分析 workspace/test.csv 并报告行数').fill('完成需求分析并实现');
  await expect(page.getByRole('button',{name:'派发',exact:true})).toBeEnabled();
  await expect(page.getByText('需求理解核验',{exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'派发',exact:true}).click();
  await expect.poll(()=>calls.length).toBe(1);
  expect(calls[0]).not.toHaveProperty('understanding_check_id');
});
