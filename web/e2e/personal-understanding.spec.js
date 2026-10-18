import {test,expect} from '@playwright/test';

test('个人 Agent 逐题沟通、解释、跳过补答与执行隔离',async({page})=>{
  const questions=Array.from({length:5},(_,i)=>({id:`Q${i+1}`,question:`个人理解题 ${i+1}`,critical:i<2,
    category:i===0?'requirement':i===1?'project':'process',options:['符合资料','错误一','错误二','错误三']}));
  const feedback={explanation:'根据原文解释本题',summary:'保留独立上下文',knowledge_points:['隔离讨论不会写入项目执行上下文'],
    source_id:'doc:project',source_title:'实现与验收',source_hash:'version1',quote:'项目采用独立记录'};
  let check=null,triggered=false;const runs=[],chats=[];
  await page.route('**/agent/status*',route=>route.fulfill({json:{personal_understanding:true,oneshot_usage:{},session_usage:{},model:'fake'}}));
  await page.route('**/agent/understanding/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace('/agent/understanding','');
    if(path==='/panel.js') return route.continue();
    const input=route.request().method()==='POST'?route.request().postDataJSON():{};
    let data={};
    if(path==='/policy') data={required:true,available:true,scope:'personal_agent',max_cost_usd:0.1};
    else if(path==='/documents') data={docs:[{doc_id:'project',title:'实现与验收'}]};
    else if(path==='/trigger')data={required:triggered,reason:'用户主动申请'};
    else if(path==='/checks'&&route.request().method()==='POST') {
      triggered=true;check={id:'uc-personal',state:'pending',requirement:input.dispatch.prompt,request:input.dispatch,questions,
        valid_now:true,expires_at:Date.now()/1000+3600,cost_usd:0.001,result:null,sources:[],
        progress:{current_question_id:'Q1',revision:0,skipped_question_ids:[],outcomes:[],discussions:[],corrections:[]}};data=check;
    } else if(path.endsWith('/question-answer')) {
      const q=questions.find(q=>q.id===input.question_id);
      check.progress.outcomes.push({...feedback,id:q.id,correct:true,expected:0,answer:0,critical:q.critical});
      check.progress.skipped_question_ids=check.progress.skipped_question_ids.filter(id=>id!==q.id);data=check;
    } else if(path.endsWith('/discussions')) {
      chats.push(input);check.progress.discussions.push({kind:'discussion_request',question_id:input.question_id,message:input.message},
        {kind:'discussion_reply',question_id:input.question_id,feedback,cost_usd:0.001});data=check;
    } else if(path.endsWith('/navigate')) {
      const p=check.progress;
      if(input.action==='finish'){check.result={passed:true,score_percent:100,critical_passed:true};check.state='passed';}
      else if(input.action==='revisit')p.current_question_id=input.target;
      else {
        if(input.action==='skip')p.skipped_question_ids.push(input.question_id);
        const ids=questions.map(q=>q.id),index=ids.indexOf(input.question_id);
        p.current_question_id=[...ids.slice(index+1),...ids.slice(0,index)].find(id=>!p.outcomes.some(o=>o.id===id))||input.question_id;
      }
      data=check;
    } else if(path.endsWith('/audit'))data={...check,schema_version:2};
    else if(path==='/checks')data={checks:check?[check]:[]};
    else if(path==='/assessments')data={assessments:[]};
    else data=check||{};
    if(check&&input.revision!==undefined)check.progress.revision++;
    await route.fulfill({json:data});
  });
  await page.route('**/agent/run/stream*',async route=>{runs.push(route.request().postDataJSON());await route.fulfill({contentType:'text/event-stream',body:'event: result\ndata: {"result":"完成","num_turns":1}\n\n'});});
  await page.goto('/panel');
  const box=page.locator('#personal-learning');await expect(box).toBeVisible();
  await box.locator(':scope > summary').click();
  await page.locator('#prompt').fill('实现导出并检查验收');
  await box.getByLabel('个人核验项目资料').selectOption('project');
  await box.getByRole('button',{name:'申请理解核验',exact:true}).click();
  const answer=async()=>{await box.locator('input[name=personal-answer][value="0"]').check();await box.getByRole('button',{name:'提交本题答案',exact:true}).click();await expect(box).toContainText('相关知识与代码原理：');};
  await expect(box.locator('legend')).toContainText('Q1');
  await answer();
  await box.locator('[data-message]').fill('为什么这样实现？');
  await box.getByRole('button',{name:'发送本题追问',exact:true}).click();
  await expect(box).toContainText('我的追问：为什么这样实现？');
  await expect(box.locator('legend')).toContainText('Q1');expect(chats[0].question_id).toBe('Q1');
  await page.locator('#btn-dispatch').click();expect(runs).toHaveLength(0);
  await box.getByRole('button',{name:'下一题',exact:true}).click();
  await box.getByRole('button',{name:'暂时跳过',exact:true}).click();
  await expect(box.getByRole('button',{name:'返回 Q2',exact:true})).toBeVisible();
  for(const id of ['Q3','Q4','Q5']) {await expect(box.locator('legend')).toContainText(id);await answer();await box.getByRole('button',{name:'下一题',exact:true}).click();}
  await expect(box.locator('legend')).toContainText('Q2');
  await expect(box.getByRole('button',{name:'完成核验',exact:true})).toBeDisabled();
  await answer();await box.getByRole('button',{name:'完成核验',exact:true}).click();
  await expect(box).toContainText('核验通过，可以继续执行');
  await page.locator('#btn-dispatch').click();await expect.poll(()=>runs.length).toBe(1);
  expect(runs[0].understanding_check_id).toBe('uc-personal');
  expect(runs[0]).not.toHaveProperty('questions');expect(runs[0]).not.toHaveProperty('answers');
  const download=page.waitForEvent('download');await box.getByRole('button',{name:'导出当前记录',exact:true}).click();
  expect((await download).suggestedFilename()).toBe('uc-personal.json');
});
test('普通个人请求不强制答题，未声明能力的节点面板不加载沟通模块',async({page})=>{
  let learningRequests=0;
  await page.route('**/agent/status*',route=>route.fulfill({json:{oneshot_usage:{},session_usage:{}}}));
  await page.route('**/agent/understanding/**',route=>{learningRequests++;return route.fulfill({status:404,json:{message:'not mounted'}});});
  await page.goto('/panel');
  await expect(page.locator('#personal-learning')).toBeHidden();
  expect(learningRequests).toBe(0);
});
