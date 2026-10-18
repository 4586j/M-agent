/* Personal Agent learning UI. Its conversation never enters the execution composer. */
(() => {
  const root = document.getElementById('personal-learning');
  if (!root) return;
  const base = '/agent/understanding';
  const h = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let policy, docs=[], check=null, body=null, key='', busy=false, epoch=0, selected=[], observed=null, activeRunKey='';
  const normalize = value => ({prompt:'',persona:null,context_memory:null,cwd:null,model:null,planning_enabled:null,
    understanding_check_id:null,session_id:null,images:null,...value,understanding_check_id:null});
  const bodyKey = value => JSON.stringify(normalize(value));
  const currentBody = () => {
    let prompt = document.getElementById('prompt').value.trim();
    let planning = document.getElementById('plan-cb').checked;
    if(prompt.startsWith('//')) prompt=prompt.slice(1);
    else if (/^\/plan\s+/i.test(prompt)) {prompt=prompt.replace(/^\/plan\s+/i,'');planning=true;}
    else if(prompt.startsWith('/')) throw new Error('请填写任务需求后开启沟通；/usage 等控制命令不作为核验需求。');
    const value = {prompt,planning_enabled:planning};
    const model=document.getElementById('model').value.trim();
    if(model) value.model=model;
    if(typeof sessionId !== 'undefined' && sessionId) value.session_id=sessionId;
    if(typeof pendingImages !== 'undefined' && pendingImages.length) value.images=[...pendingImages];
    return value;
  };
  async function api(path, data) {
    const response=await fetch(apiUrl(base+path), data === undefined ? {} : {
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const value=await response.json();
    if(!response.ok) throw new Error(value.message || value.detail || `HTTP ${response.status}`);
    return value;
  }
  function notice(text) {root.querySelector('[data-notice]').textContent=text;}
  const money = cost => cost == null ? '待核对' : '$'+Number(cost).toFixed(4);
  function feedback(value) {
    return `<div class="learning-feedback"><p><b>解释：</b>${h(value.explanation)}</p><p><b>总结：</b>${h(value.summary)}</p>`+
      `<p><b>相关知识与代码原理：</b></p><ul>${(value.knowledge_points||[]).map(p=>`<li>${h(p)}</li>`).join('')}</ul>`+
      (value.evidence||[{quote:value.quote,source_id:value.source_id,source_title:value.source_title,source_hash:value.source_hash}])
      .map(e=>`<blockquote><b>依据：</b>${h(e.quote)}<small>${h(e.source_title)} · ${h(e.source_id)} · 版本 ${h(e.source_hash)}</small></blockquote>`).join('')+'</div>';
  }
  function render() {
    const area=root.querySelector('[data-question]');
    if(!check) {area.innerHTML='';return;}
    const p=check.progress,q=check.questions.find(q=>q.id===p.current_question_id), outcome=p.outcomes.find(o=>o.id===q?.id);
    if(!q) {area.textContent=check.error||'本轮未生成有效题目，请核查资料后重试。';return;}
    area.innerHTML=`<p>本轮 ${h(check.id)} · 已答 ${p.outcomes.length}/${check.questions.length} · 出题估算 ${money(check.cost_usd)}</p>`+
      `<fieldset><legend>${h(q.id)} ${q.critical?'【关键题】':''}${h(q.question)}</legend>`+
      q.options.map((o,i)=>`<label class="learning-option"><input type="radio" name="personal-answer" value="${i}" ${outcome?.answer===i?'checked':''} ${outcome||check.result?'disabled':''}>${h(o)}</label>`).join('')+'</fieldset>'+
      (outcome ? `<p>${outcome.correct?'回答正确':'回答有误，需要复习'} · 正确选项：${h(q.options[outcome.expected])}</p>${feedback(outcome)}` : '<button type="button" data-action="answer">提交本题答案</button>')+
      p.discussions.filter(e=>e.question_id===q.id).map(e=>e.kind==='discussion_request'?`<p><b>我的追问：</b>${h(e.message)}</p>`:
        (e.feedback?feedback(e.feedback):`<p role="alert">${h(e.error||'生成未完成')}</p>`)+`<small>本次追问估算 ${money(e.cost_usd)}</small>`).join('')+
      (outcome ? '<label>继续讨论本题<textarea data-message maxlength="1500" placeholder="只讨论当前题，不会自动进入下一题"></textarea></label><button type="button" data-action="discuss">发送本题追问</button>':'')+
      `<div class="learning-actions">${!check.result && outcome && p.outcomes.length<check.questions.length?'<button type="button" data-action="next">下一题</button>':''}`+
      `${!check.result && !outcome?'<button type="button" data-action="skip">暂时跳过</button>':''}`+
      `${!check.result?`<button type="button" data-action="finish" ${p.outcomes.length!==check.questions.length?'disabled':''}>完成核验</button>`:''}`+
      `<button type="button" data-action="audit">导出当前记录</button></div>`+
      (p.skipped_question_ids.length?'<p>待补答：'+p.skipped_question_ids.map(id=>`<button type="button" data-action="revisit" data-target="${h(id)}">返回 ${h(id)}</button>`).join('')+'</p>':'')+
      (p.outcomes.length?'<p>复习：'+p.outcomes.map(o=>`<button type="button" data-action="revisit" data-target="${h(o.id)}">复习 ${h(o.id)}</button>`).join('')+'</p>':'')+
      (check.result?`<p role="status">${check.result.passed&&check.valid_now?'核验通过，可以继续执行':'未通过或已失效，请复习后重新发起'} · 正确率 ${check.result.score_percent}% · 关键题${check.result.critical_passed?'全部正确':'未全部正确'}</p>`:'')+
      (outcome?'<details><summary>AI 理解有误？记录纠正参考</summary><label>纠正内容<textarea data-claim maxlength="1500"></textarea></label><label>所选第一份资料中的依据原文<textarea data-quote maxlength="1000"></textarea></label><button type="button" data-action="correct">保存待核实意见</button></details>':'')+
      (p.corrections||[]).filter(e=>e.kind==='correction_proposed'&&e.question_id===q.id).map(e=>{
        const verified=p.corrections.some(v=>v.kind==='correction_verified'&&v.correction_id===e.correction_id);
        return `<p>${h(e.claim)} · ${verified?'已核实，可供分析参考':'待核实，尚不用于分析'}</p><blockquote>${h(e.quote)}</blockquote>`+
          (verified?'':`<button type="button" data-action="verify" data-target="${h(e.correction_id)}">已核对原文，采用为参考</button>`);
      }).join('');
  }
  async function operate(fn) {
    if(busy) return;
    const token=++epoch;busy=true;root.inert=true;root.setAttribute('aria-busy','true');notice('正在处理…');
    try {await fn(); if(token===epoch) notice('');}
    catch(e) {if(token===epoch) notice(e.message||String(e));}
    finally {if(token===epoch){busy=false;root.inert=false;root.setAttribute('aria-busy','false');}}
  }
  async function refreshDocs() {
    docs=(await api('/documents')).docs;
    root.querySelector('[data-documents]').innerHTML=docs.map(d=>`<option value="${h(d.doc_id)}" ${selected.includes(d.doc_id)?'selected':''}>${h(d.title)}</option>`).join('');
  }
  async function download(path, id) {
    const value=await api(path), url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=id+'.json';link.click();URL.revokeObjectURL(url);
  }
  root.innerHTML=`<summary>人机理解沟通（个人单 Agent）</summary><div class="learning-content"><p>按需开启：你主动申请，或模型发现有依据的严重理解偏差。每题作答后给出解释、总结、依据与知识点；由你决定下一题，也可暂时跳过。</p>`+
    `<p>每次出题、追问或偏差分析的 SDK 预算 ${money(policy?.max_cost_usd)}；问答独立保存，不写入执行会话。</p>`+
    '<label>项目实现和验收资料（选择 1–3 份）<select data-documents multiple aria-label="个人核验项目资料"></select></label>'+
    '<details><summary>添加本项目参考资料</summary><label>资料标题<input data-title maxlength="200"></label><label>资料正文<textarea data-content maxlength="12000"></textarea></label><button type="button" data-action="add-document">保存资料</button></details>'+
    '<div class="learning-actions"><button type="button" data-action="start">申请理解核验</button><button type="button" data-action="assess">分析理解偏差</button><button type="button" data-action="history">学习历史与核查</button></div>'+
    '<p data-notice role="status"></p><div data-assessment></div><div data-question></div><div data-history></div></div>';
  root.addEventListener('change',e=>{if(e.target.matches('[data-documents]')) selected=Array.from(e.target.selectedOptions,o=>o.value);});
  root.addEventListener('click',event=>{
    const button=event.target.closest('button[data-action]');if(!button) return;
    const action=button.dataset.action,target=button.dataset.target;
    void operate(async()=>{
      if(action==='add-document') {await api('/documents',{title:root.querySelector('[data-title]').value,content:root.querySelector('[data-content]').value});await refreshDocs();return;}
      if(action==='history') {
        const [history,assessments]=await Promise.all([api('/checks'),api('/assessments')]);
        root.querySelector('[data-history]').innerHTML=(history.checks||[]).map(c=>`<p>${h(c.requirement)} · ${h(c.state)} <button type="button" data-action="resume" data-target="${h(c.id)}">查看本轮</button> <button type="button" data-action="history-audit" data-target="${h(c.id)}">导出核查</button></p>`).join('')+
          (assessments.assessments||[]).map(a=>`<p>${h(a.prompt)} · ${h(a.severity)} · ${h(a.reason)} <button type="button" data-action="assessment-audit" data-target="${h(a.id)}">导出偏差分析</button></p>`).join('');return;
      }
      if(action==='history-audit'||action==='assessment-audit') return download(`/${action==='history-audit'?'checks':'assessments'}/${encodeURIComponent(target)}/audit`,target);
      if(action==='resume') {check=await api('/checks/'+encodeURIComponent(target));body=check.request;key=bodyKey(body);selected=check.sources.filter(s=>s.id.startsWith('doc:')).map(s=>s.id.slice(4));await refreshDocs();render();return;}
      if(action==='start'||action==='assess') {
        const next=currentBody();if(!next.prompt) throw new Error('请先填写要讨论的需求');
        if(bodyKey(next)!==key){check=null;body=next;key=bodyKey(next);}
        if(action==='start'){check=await api('/checks',{dispatch:body,document_ids:selected});render();}
        else {
          const result=await api('/assess',{dispatch:body,document_ids:selected,
            analysis:observed?.key===key?observed.text:''});
          root.querySelector('[data-assessment]').textContent=(result.severity==='serious'?'发现严重理解偏差，须核验后执行：':result.severity==='normal'?'未发现有充分依据的严重偏差：':'分析未完成：')+result.reason+' · '+money(result.cost_usd);
        }
        return;
      }
      if(!check) return;
      const p=check.progress,q=p.current_question_id, path='/checks/'+encodeURIComponent(check.id);
      if(action==='audit') return download(path+'/audit',check.id);
      if(action==='answer') {
        const input=root.querySelector('input[name="personal-answer"]:checked');if(!input) throw new Error('请选择本题答案');
        check=await api(path+'/question-answer',{question_id:q,answer:Number(input.value),revision:p.revision});
      } else if(action==='discuss') check=await api(path+'/discussions',{question_id:q,message:root.querySelector('[data-message]').value,revision:p.revision});
      else if(action==='correct') check=await api(path+'/corrections',{question_id:q,claim:root.querySelector('[data-claim]').value,quote:root.querySelector('[data-quote]').value,document_id:selected[0]||'',revision:p.revision});
      else if(action==='verify') check=await api(path+'/verify-correction',{question_id:q,correction_id:target,revision:p.revision});
      else check=await api(path+'/navigate',{question_id:q,action,target,revision:p.revision});
      render();
    });
  });
  window.agentLearning={
    observe(result) {if(activeRunKey) observed={key:activeRunKey,text:String(result.result||result.output||'').slice(0,3000)};},
    async allow(next) {
      if(busy){root.open=true;notice('请等待当前沟通操作完成');return false;}
      if(bodyKey(next)!==key){check=null;body=next;key=bodyKey(next);render();}
      const state=await api('/trigger',{dispatch:next});
      if(!state.required){activeRunKey=bodyKey(next);return true;}
      if(check?.state==='passed'&&check.valid_now&&Date.now()/1000<check.expires_at){activeRunKey=bodyKey(next);next.understanding_check_id=check.id;return true;}
      root.open=true;notice(state.reason+'。请完成核验后再执行。');root.scrollIntoView({block:'nearest'});return false;
    }
  };
  api('/policy').then(value=>{policy=value;root.querySelector('.learning-content > p:nth-child(2)').textContent=`每次出题、追问或偏差分析的 SDK 预算 ${money(policy.max_cost_usd)}；评分和切题不调用模型。`;return refreshDocs();}).catch(e=>notice(e.message));
})();
