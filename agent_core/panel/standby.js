import {createBloomPainter} from './bloom.js';

// Shared by the personal panel and the React orchestration shell. No API or
// task/session state is read or written. The native modal lives outside both apps.
export function mountStandby({target,scope='personal'}={}) {
  if(!target)throw new Error('Standby controls require a mount target');
  const key=`m-agent.standby.${scope}.settings`,sessionKey=`m-agent.standby.${scope}.tab`;
  const defaults={seconds:300,variant:'lines',brightness:85,clock:true,reduced:null};
  const normalize=value=>({
    seconds:[0,60,300,600,900].includes(value?.seconds)?value.seconds:300,
    variant:value?.variant==='particles'?'particles':'lines',
    brightness:Number.isFinite(value?.brightness)?Math.max(35,Math.min(100,value.brightness)):85,
    clock:typeof value?.clock==='boolean'?value.clock:true,
    reduced:typeof value?.reduced==='boolean'?value.reduced:null,
  });
  let settings={...defaults};
  try{settings=normalize(JSON.parse(localStorage.getItem(key)));}catch{/* Storage is optional. */}
  const host=document.createElement('span');host.dataset.agentStandbyControls=scope;target.append(host);
  const shadow=host.attachShadow({mode:'open'});
  shadow.innerHTML=`<style>
    :host{display:inline-flex;font:12px "Segoe UI","Microsoft YaHei",sans-serif;color:#cedbe4;line-height:1.5;color-scheme:dark}
    *{box-sizing:border-box}button,select,input{font:inherit}button{cursor:pointer}button,summary,input,select{-webkit-tap-highlight-color:transparent}button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #9bcacb;outline-offset:4px}[hidden]{display:none!important}
    summary{list-style:none;border:1px solid #8396aa44;border-radius:8px;padding:6px 11px;cursor:pointer;white-space:nowrap;background:#172332;color:#cfdee6}summary::-webkit-details-marker{display:none}
    .menu{position:fixed;inset:auto 18px auto auto;top:65px;margin:0;width:min(290px,calc(100vw - 24px));padding:19px;background:#14222e;border:1px solid #6c8c9a66;border-radius:12px;box-shadow:0 15px 50px #0006;z-index:2147483646;color:#d9e6ec}
    h3{margin:0 0 14px;font-size:14px;font-weight:500}label{display:flex;align-items:center;justify-content:space-between;gap:15px;margin:13px 0}select{max-width:155px;border:1px solid #728e9b55;background:#0c1924;color:#d9e6ec;padding:5px;border-radius:5px}input[type=range]{width:125px;accent-color:#93c5bd}input[type=checkbox]{accent-color:#93c5bd}small{display:block;color:#98aeb9;font-size:11px;line-height:1.8}.enter{width:100%;margin:12px 0 9px;background:#aecfc7;color:#152c34;border:0;border-radius:7px;padding:9px}
    @media(max-width:760px){:host{position:fixed;right:12px;top:12px;z-index:2147483645}}
  </style><details><summary aria-label="待机设置">◌ 待机</summary><div class="menu" popover="auto"><h3>呼吸花园</h3>
    <label>闲置后待机<select aria-label="闲置后待机"><option value="60">1 分钟</option><option value="300">5 分钟</option><option value="600">10 分钟</option><option value="900">15 分钟</option><option value="0">关闭自动待机</option></select></label>
    <label>表现方式<select aria-label="花园表现方式"><option value="lines">流线花冠</option><option value="particles">星尘花园</option></select></label>
    <label>画面亮度<input type="range" aria-label="待机画面亮度" min="35" max="100"></label>
    <label>显示时钟<input type="checkbox" aria-label="待机显示时钟"></label><label>减少动态<input type="checkbox" aria-label="待机减少动态"></label>
    <button type="button" class="enter">立即待机</button><small>遮挡画面，后台任务继续运行。<br>设置保存在本机，仅影响当前界面。</small></div></details>`;
  const details=shadow.querySelector('details'),summary=shadow.querySelector('summary');
  const menu=shadow.querySelector('.menu');
  const secondsInput=shadow.querySelector('[aria-label="闲置后待机"]');
  const variantInput=shadow.querySelector('[aria-label="花园表现方式"]');
  const brightnessInput=shadow.querySelector('[aria-label="待机画面亮度"]');
  const clockInput=shadow.querySelector('[aria-label="待机显示时钟"]');
  const reducedInput=shadow.querySelector('[aria-label="待机减少动态"]');

  // Separate body child + native top layer: transformed/overflowing app shells
  // and React portals cannot draw over this opaque modal.
  const overlay=document.createElement('div');overlay.dataset.agentStandbyOverlay=scope;
  const view=overlay.attachShadow({mode:'open'});document.body.append(overlay);
  view.innerHTML=`<style>
    :host{font:13px "Segoe UI","Microsoft YaHei",sans-serif;color-scheme:dark}*{box-sizing:border-box}
    dialog{position:fixed;inset:0;width:100vw;height:100vh;height:100dvh;max-width:none;max-height:none;margin:0;border:0;padding:0;overflow:hidden;background:#0b1723;color:#e1eeeb;isolation:isolate}
    dialog::backdrop{background:#0b1723}canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
    header{position:absolute;left:6vw;right:6vw;top:5vh;display:flex;justify-content:space-between;align-items:center;pointer-events:none}.brand{font-size:32px;font-weight:500;letter-spacing:-3px;opacity:.55}.private{font-size:11px;color:#b0c7cc}
    .clock{position:absolute;left:9vw;top:27vh;pointer-events:none}time{display:block;font-size:clamp(70px,10vw,150px);font-weight:250;letter-spacing:-6px;line-height:1.15;font-variant-numeric:tabular-nums}.date{font-size:12px;letter-spacing:2px;opacity:.55;margin-top:14px}.poem{font-size:13px;letter-spacing:2px;opacity:.5;margin-top:35px;line-height:1.8}
    footer{position:absolute;bottom:max(5vh,20px);left:6vw;right:6vw;display:flex;justify-content:space-between;align-items:center;gap:20px}.title{min-width:190px;font-size:12px;opacity:.65}.title span{display:block;font-size:10px;margin-top:6px;opacity:.7}.return{text-align:center}.restore{position:relative;overflow:hidden;min-width:215px;height:47px;background:#ffffff0a;color:inherit;border:1px solid #ffffff30;border-radius:10px;user-select:none;-webkit-user-select:none;touch-action:none;cursor:pointer;font:inherit}.restore .progress{position:absolute;inset:0;transform:scaleX(0);transform-origin:left;background:#ffffff28;pointer-events:none}.restore .copy{position:relative;pointer-events:none}.help{font-size:10px;opacity:.45;margin-top:10px}.hint{min-width:190px;text-align:right;font-size:11px;opacity:.45}.restore:focus-visible{outline:2px solid #b1ddd1;outline-offset:6px}
    .no-clock time,.no-clock .date{visibility:hidden}
    @media(max-width:760px){.clock{left:8vw;top:23vh}time{font-size:85px;letter-spacing:-4px}.date{font-size:11px;letter-spacing:1px}.poem{font-size:11px;letter-spacing:1px;max-width:245px}.private{font-size:10px}footer{flex-direction:column;gap:22px}.title{text-align:center;min-width:0}.hint{display:none}.restore{min-width:230px}}
  </style><dialog aria-label="呼吸花园待机画面" aria-describedby="privacy-note"><canvas aria-hidden="true"></canvas>
    <header><span class="brand" aria-hidden="true">m·</span><span id="privacy-note" class="private">工作区画面已遮挡</span></header>
    <div class="clock"><time></time><div class="date"></div><p class="poem">不必着急，一切都在慢慢生长。</p></div>
    <footer><div class="title">呼吸花园<span class="variant-name"></span></div><div class="return"><button type="button" class="restore" aria-label="按住返回工作区，或按 Enter 确认"><span class="progress"></span><span class="copy">按住返回工作区 ↵</span></button><div class="help">或聚焦按钮后按 Enter 确认</div></div><div class="hint">移动轻拂 · 轻点涟漪 · R 唤起</div></footer>
  </dialog>`;
  const dialog=view.querySelector('dialog'),canvas=view.querySelector('canvas'),restore=view.querySelector('.restore');
  const ctx=canvas.getContext('2d'),media=matchMedia('(prefers-reduced-motion: reduce)');
  const paintBloom=createBloomPainter(()=>settings.variant);
  const surface={w:0,h:0,dpr:1,interaction:{x:.66,y:.53,targetX:.66,targetY:.53,strength:0,active:false,ripples:[]}};
  const listeners=[],hiddenNodes=new Map();
  let covered=false,destroyed=false,previousFocus=null,lastActivity=Date.now(),time=0,raf=0,lastTick=0,lastPaint=0,budget=0;
  let holdRaf=0,holdStarted=0,holdPointer=null,oldOverflow='',tabState=null;
  const reduced=()=>settings.reduced??media.matches;
  function listen(node,name,handler,options){node.addEventListener(name,handler,options);listeners.push(()=>node.removeEventListener(name,handler,options));}
  listen(details,'toggle',()=>{if(details.open){if(!menu.matches(':popover-open'))menu.showPopover();}else if(menu.matches(':popover-open'))menu.hidePopover();});
  listen(menu,'toggle',event=>{if(event.newState==='closed')details.open=false;});
  function clearInteraction(){Object.assign(surface.interaction,{strength:0,active:false,ripples:[]});}
  function persistTab(){try{sessionStorage.setItem(sessionKey,JSON.stringify({covered,lastActivity}));}catch{/* In-memory fallback. */}}
  function paint(){
    if(!covered||!ctx||!surface.w||!surface.h||document.hidden)return;
    ctx.save();ctx.setTransform(surface.dpr,0,0,surface.dpr,0,0);paintBloom(ctx,surface.w,surface.h,time,false,surface);ctx.restore();
  }
  function resize(){
    if(!covered)return;
    surface.w=dialog.clientWidth;surface.h=dialog.clientHeight;surface.dpr=Math.min(devicePixelRatio||1,2);
    canvas.width=Math.round(surface.w*surface.dpr);canvas.height=Math.round(surface.h*surface.dpr);paint();
  }
  function animate(now){
    raf=0;if(!covered||document.hidden||reduced()||destroyed)return;
    budget+=Math.min(now-lastTick,80);lastTick=now;
    if(budget>=1000/60-.5){
      budget=Math.max(0,budget-1000/60);const dt=Math.min((now-lastPaint)/1000,.08);lastPaint=now;time+=dt*1.07;
      const i=surface.interaction,blend=1-Math.exp(-dt*8);
      i.x+=(i.targetX-i.x)*blend;i.y+=(i.targetY-i.y)*blend;i.strength+=((i.active?1:0)-i.strength)*blend;
      for(const ripple of i.ripples)ripple.age+=dt;i.ripples=i.ripples.filter(r=>r.age<1.8);paint();
    }
    raf=requestAnimationFrame(animate);
  }
  function syncAnimation(){
    cancelAnimationFrame(raf);raf=0;clearInteraction();lastTick=lastPaint=performance.now();budget=0;paint();
    if(covered&&!document.hidden&&!reduced())raf=requestAnimationFrame(animate);
  }
  function updateClock(){
    if(!covered)return;
    const now=new Date(),el=view.querySelector('time');el.textContent=now.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});el.dateTime=now.toISOString();
    view.querySelector('.date').textContent=now.toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
  }
  function hideNode(node){
    if(node===overlay||!(node instanceof HTMLElement)||hiddenNodes.has(node))return;
    hiddenNodes.set(node,{inert:node.inert,aria:node.getAttribute('aria-hidden')});node.inert=true;node.setAttribute('aria-hidden','true');
  }
  const observer=new MutationObserver(records=>{if(covered)for(const record of records)for(const node of record.addedNodes)hideNode(node);});
  function notify(){
    if(covered)document.documentElement.dataset.agentStandby='true';else delete document.documentElement.dataset.agentStandby;
    document.dispatchEvent(new CustomEvent('m-agent:standby',{detail:{covered}}));
  }
  function enter(){
    if(covered||destroyed)return;
    previousFocus=document.activeElement;
    while(previousFocus?.shadowRoot?.activeElement)previousFocus=previousFocus.shadowRoot.activeElement;
    details.open=false;covered=true;oldOverflow=document.body.style.overflow;
    dialog.showModal();restore.focus();
    for(const node of document.body.children)hideNode(node);
    observer.observe(document.body,{childList:true});document.body.style.overflow='hidden';notify();persistTab();updateClock();resize();syncAnimation();
  }
  function endHold(){cancelAnimationFrame(holdRaf);holdRaf=0;holdStarted=0;holdPointer=null;view.querySelector('.progress').style.transform='scaleX(0)';}
  function leave(){
    if(!covered)return;
    endHold();cancelAnimationFrame(raf);raf=0;covered=false;clearInteraction();observer.disconnect();
    for(const [node,before] of hiddenNodes){node.inert=before.inert;if(before.aria===null)node.removeAttribute('aria-hidden');else node.setAttribute('aria-hidden',before.aria);}hiddenNodes.clear();
    document.body.style.overflow=oldOverflow;dialog.close();lastActivity=Date.now();persistTab();notify();
    if(previousFocus?.isConnected&&previousFocus.getClientRects().length&&!previousFocus.closest('[inert]'))previousFocus.focus();else summary.focus();
  }
  function checkIdle(){if(!covered&&settings.seconds>0&&Date.now()-lastActivity>=settings.seconds*1000)enter();}
  function applySettings(){
    secondsInput.value=String(settings.seconds);variantInput.value=settings.variant;brightnessInput.value=String(settings.brightness);clockInput.checked=settings.clock;reducedInput.checked=reduced();
    canvas.style.filter=`brightness(${settings.brightness/100})`;dialog.classList.toggle('no-clock',!settings.clock);
    view.querySelector('.variant-name').textContent=settings.variant==='lines'?'流线花冠':'星尘花园';syncAnimation();
  }
  function saveSettings(){try{localStorage.setItem(key,JSON.stringify(settings));}catch{/* In-memory fallback. */}applySettings();}
  for(const [input,field] of [[secondsInput,'seconds'],[brightnessInput,'brightness']])listen(input,'input',()=>{settings[field]=Number(input.value);if(field==='seconds')lastActivity=Date.now();saveSettings();});
  listen(variantInput,'change',()=>{settings.variant=variantInput.value;saveSettings();});
  listen(clockInput,'change',()=>{settings.clock=clockInput.checked;saveSettings();});
  listen(reducedInput,'change',()=>{settings.reduced=reducedInput.checked;saveSettings();});
  listen(media,'change',()=>{if(settings.reduced===null)applySettings();});
  listen(shadow.querySelector('.enter'),'click',enter);
  listen(document,'pointerdown',event=>{if(!event.composedPath().includes(host))details.open=false;},{passive:true});
  listen(shadow,'keydown',event=>{if(event.key==='Escape'){details.open=false;summary.focus();event.stopPropagation();}});
  listen(dialog,'cancel',event=>event.preventDefault());
  listen(dialog,'close',()=>{if(covered)dialog.showModal();});
  function ripple(x=.66,y=.53){if(!covered||reduced())return;const list=surface.interaction.ripples;if(list.length>=4)list.shift();list.push({x,y,age:0});}
  function locate(event){
    if(!covered||reduced()||event.isPrimary===false||event.composedPath().includes(restore))return null;
    const r=dialog.getBoundingClientRect(),x=Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),y=Math.max(0,Math.min(1,(event.clientY-r.top)/r.height));
    Object.assign(surface.interaction,{targetX:x,targetY:y,active:true});return{x,y};
  }
  listen(dialog,'pointermove',event=>{if(!locate(event))surface.interaction.active=false;},{passive:true});
  listen(dialog,'pointerdown',event=>{if(event.button!==0)return;const p=locate(event);if(p)ripple(p.x,p.y);},{passive:true});
  for(const type of ['pointerleave','pointercancel'])listen(dialog,type,()=>surface.interaction.active=false);
  listen(dialog,'pointerup',event=>{if(event.pointerType!=='mouse')surface.interaction.active=false;});
  listen(restore,'pointerdown',event=>{
    if(event.button!==0||event.isPrimary===false)return;
    event.preventDefault();endHold();holdStarted=performance.now();holdPointer=event.pointerId;restore.setPointerCapture(event.pointerId);
    const tick=now=>{const progress=Math.min(1,(now-holdStarted)/650);view.querySelector('.progress').style.transform=`scaleX(${progress})`;if(progress>=1)leave();else holdRaf=requestAnimationFrame(tick);};holdRaf=requestAnimationFrame(tick);
  });
  listen(restore,'pointermove',event=>{if(event.pointerId!==holdPointer)return;const r=restore.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)endHold();});
  for(const type of ['pointerup','pointercancel','lostpointercapture'])listen(restore,type,endHold);
  listen(restore,'click',event=>{if(event.detail===0)leave();});
  // Capture before application shortcuts; typing into the artwork cannot dispatch
  // a command or open a background portal. Browser shortcuts remain available.
  listen(document,'keydown',event=>{
    if(!covered)return;
    event.stopImmediatePropagation();
    if(event.key==='Escape'){event.preventDefault();return;}
    if(event.key==='Tab'){event.preventDefault();restore.focus();return;}
    if((event.key==='Enter'||event.key===' ')&&view.activeElement===restore){event.preventDefault();leave();return;}
    if(event.key.toLowerCase()==='r'&&!event.repeat&&!event.ctrlKey&&!event.metaKey&&!event.altKey){event.preventDefault();ripple();}
  },true);
  for(const type of ['pointermove','pointerdown','keydown','wheel','touchstart','input'])listen(document,type,event=>{if(!covered&&event.isTrusted)lastActivity=Date.now();},{passive:true,capture:true});
  listen(document,'visibilitychange',()=>{checkIdle();persistTab();syncAnimation();});
  listen(window,'focus',checkIdle);
  listen(window,'pageshow',()=>{checkIdle();syncAnimation();});
  listen(window,'pagehide',persistTab);
  listen(window,'resize',resize);
  listen(window,'storage',event=>{if(event.key===key){try{settings=normalize(JSON.parse(event.newValue));applySettings();checkIdle();}catch{/* Ignore corrupt optional preference. */}}});
  const idleTimer=setInterval(checkIdle,1000),clockTimer=setInterval(updateClock,10000);
  try{tabState=JSON.parse(sessionStorage.getItem(sessionKey));}catch{/* No tab state. */}
  if(Number.isFinite(tabState?.lastActivity)&&tabState.lastActivity<=Date.now())lastActivity=tabState.lastActivity;
  applySettings();if(tabState?.covered===true)enter();else checkIdle();
  delete document.documentElement.dataset.agentStandbyBoot;
  if(covered)restore.focus();
  return {enter,destroy(){
    if(destroyed)return;
    const resume={covered,lastActivity};leave();
    try{sessionStorage.setItem(sessionKey,JSON.stringify(resume));}catch{/* Keep cleanup independent of storage. */}
    destroyed=true;endHold();cancelAnimationFrame(raf);observer.disconnect();clearInterval(idleTimer);clearInterval(clockTimer);for(const remove of listeners)remove();host.remove();overlay.remove();
  }};
}
