/* Standalone concept lab. No application data, external assets or model calls. */
(() => {
  'use strict';
  const scenes = [
    {id:'orbit',name:'引力编队',note:'安静的科技感',tag:'推荐',description:'节点绕着同一个重心，安静地继续运转。',poem:'让思绪，绕一小段远路。'},
    {id:'aurora',name:'极光织幕',note:'流动与沉浸',tag:'',description:'细细的光线织成幕布，缓慢掠过屏幕。',poem:'此刻，不必追赶任何一束光。'},
    {id:'ink',name:'水墨远山',note:'东方留白',tag:'',description:'远山隐入雾里，让紧绷的视线松下来。',poem:'山河入静，留一点空白给自己。'},
    {id:'bloom',name:'呼吸花园',note:'流线与星尘',tag:'',description:'细线缓缓舒展，微光沿着花冠轻轻游走。',poem:'不必着急，一切都在慢慢生长。'},
    {id:'matrix',name:'折光矩阵',note:'克制的建筑感',tag:'',description:'一片会呼吸的几何地景，让秩序有了温度。',poem:'在秩序之间，遇见一点温柔。'},
  ];
  const $ = id => document.getElementById(id);
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  const state = {scene:'orbit',bloomVariant:'lines',intensity:.6,brightness:.85,reduced:media.matches,paused:false,
    covered:false,lastActivity:Date.now(),idleSeconds:300,simulation:null,time:0};
  const surfaces = [];
  let frame = 0, lastFrame = 0, lastPaint = 0, frameBudget = 0, lastThumbPaint = -Infinity;
  let toastTimeout = 0, holdStarted = 0, holdFrame = 0, previousFocus = null;
  const TAU = Math.PI * 2;
  const seed = n => { const v = Math.sin(n * 127.1 + 311.7) * 43758.5453; return v - Math.floor(v); };
  const starMap = Array.from({length:66},(_,i)=>({x:seed(i+1),y:seed(i+83),size:.45+seed(i+129)*.7,phase:seed(i+300)*TAU}));
  const bloomParticles=Array.from({length:900},(_,i)=>({
    progress:seed(i+900),speed:.009+seed(i+1200)*.008,petalOffset:(seed(i+1700)-.5)*.36,
    offset:seed(i+2000)-.5,phase:seed(i+2400)*TAU,alpha:.15+seed(i+2700)*.5,size:.38+seed(i+3000)*.85,
  }));
  const bloomGlints=Array.from({length:32},(_,i)=>({progress:seed(i+700),speed:.025+seed(i+50)*.012}));

  function radial(ctx,x,y,r,stops) {
    const g = ctx.createRadialGradient(x,y,0,x,y,Math.max(1,r));
    for (const [position,color] of stops) g.addColorStop(position,color);
    return g;
  }
  function backdrop(ctx,w,h,base,color,x=.65,y=.5) {
    ctx.fillStyle=base;ctx.fillRect(0,0,w,h);
    ctx.fillStyle=radial(ctx,w*x,h*y,Math.max(w,h)*.72,[[0,color],[1,base]]);ctx.fillRect(0,0,w,h);
  }
  function stars(ctx,w,h,t,opacity=.28) {
    for (const star of starMap) {
      ctx.fillStyle=`rgba(206,223,248,${opacity*(.55+.35*Math.sin(t*.3+star.phase))})`;
      ctx.beginPath();ctx.arc(star.x*w,star.y*h,star.size,0,TAU);ctx.fill();
    }
  }
  function orb(ctx,x,y,r,color,glow=1) {
    if (glow) { ctx.fillStyle=radial(ctx,x,y,r*5,[[0,color+'80'],[.3,color+'25'],[1,color+'00']]);ctx.beginPath();ctx.arc(x,y,r*5,0,TAU);ctx.fill(); }
    ctx.fillStyle=radial(ctx,x-r*.28,y-r*.3,r*1.5,[[0,'#fffdf7'],[.35,color],[1,'#5e7892']]);
    ctx.beginPath();ctx.arc(x,y,r,0,TAU);ctx.fill();
  }
  function orbit(ctx,w,h,t,mini) {
    const u=Math.min(w,h),cx=w*((mini ? 0.5 : 0.66)),cy=h*.54;
    backdrop(ctx,w,h,'#081322','#1b304d');stars(ctx,w,h,t);
    ctx.fillStyle=radial(ctx,cx,cy,u*.4,[[0,'#7086ba17'],[1,'#7086ba00']]);ctx.fillRect(0,0,w,h);
    const dots=[];
    for(let i=0;i<6;i++) {
      const radius=u*(.17+i*.044),angle=i*.43-.8+Math.sin(t*.06)*.05;
      ctx.save();ctx.translate(cx,cy);ctx.rotate(angle);
      ctx.strokeStyle=i%2?'#9babc02c':'#b1c2dd3d';ctx.lineWidth=(mini ? 0.5 : 0.75);
      ctx.beginPath();ctx.ellipse(0,0,radius,radius*(.3+i*.015),0,0,TAU);ctx.stroke();
      const a=t*(.13+i*.014)*(i%2?-1:1)+i*1.64;
      ctx.strokeStyle=i%2?'#c0d7e989':'#e1ba7699';ctx.lineWidth=1.3;
      ctx.beginPath();ctx.ellipse(0,0,radius,radius*(.3+i*.015),0,a-.28,a);ctx.stroke();ctx.restore();
      const dx=Math.cos(a)*radius,dy=Math.sin(a)*radius*(.3+i*.015);
      dots.push({x:cx+dx*Math.cos(angle)-dy*Math.sin(angle),y:cy+dx*Math.sin(angle)+dy*Math.cos(angle),r:u*((mini ? 0.019 : 0.009))+(i%3)*u*.0015,color:i%2?'#a2c8ea':'#e1be8a'});
    }
    ctx.fillStyle=radial(ctx,cx,cy,u*.13,[[0,'#f0d6b32d'],[1,'#b1ccff00']]);ctx.beginPath();ctx.arc(cx,cy,u*.13,0,TAU);ctx.fill();
    orb(ctx,cx,cy,u*.035,'#efdbc0');for(const d of dots) orb(ctx,d.x,d.y,d.r,d.color,mini?0:1);
  }
  function aurora(ctx,w,h,t,mini) {
    backdrop(ctx,w,h,'#08161f','#153d4b',.6,.6);stars(ctx,w,h,t,.12);
    ctx.save();ctx.globalCompositeOperation='screen';
    const ribbons=mini?24:60;
    for(let i=0;i<ribbons;i++) {
      const p=i/(ribbons-1),hue=168+p*62;
      const grad=ctx.createLinearGradient(0,0,w,h);
      grad.addColorStop(0,'#2c777800');grad.addColorStop(.3,`hsla(${hue},62%,65%,${(mini ? 0.13 : 0.09)})`);
      grad.addColorStop(.64,`hsla(${hue+20},72%,73%,.25)`);grad.addColorStop(1,'#d3a0b500');
      ctx.strokeStyle=grad;ctx.lineWidth=mini?1:1.2;ctx.beginPath();
      for(let j=0;j<=90;j++) {
        const x=j/90*w*1.25-w*.1,nx=x/w;
        const envelope=Math.sin((nx+.1)*2.5);
        const y=h*(.61+Math.sin(nx*4+t*.12+p*.85)*.16+Math.sin(nx*7.7-t*.16+p*2.8)*.075)+p*h*.1*envelope;
        if(j===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
      }
      ctx.stroke();
    }
    for(let i=0;i<3;i++) {
      ctx.fillStyle=radial(ctx,w*(.38+i*.22),h*(.65+Math.sin(t*.08+i)*.1),h*.28,[[0,i===2?'#a875a512':'#63d3b918'],[1,'#63d3b900']]);ctx.fillRect(0,0,w,h);
    }
    ctx.restore();
  }
  function mountain(x,phase,layer) {
    return Math.sin(x*5.2+phase+layer*.9)*.48+Math.sin(x*11.7+phase*.43+layer*2)*.23+Math.cos(x*22.5+layer)*.09;
  }
  function ink(ctx,w,h,t,mini) {
    backdrop(ctx,w,h,'#e5ebed','#d5e1e3',.65,.7);
    const moonX=w*((mini ? 0.73 : 0.77)),moonY=h*.25,r=Math.min(w,h)*.075;
    ctx.fillStyle=radial(ctx,moonX,moonY,r*2,[[0,'#fafbf4'],[.49,'#f9fbf0c0'],[.53,'#f9fbf055'],[1,'#f9fbf000']]);ctx.beginPath();ctx.arc(moonX,moonY,r*2,0,TAU);ctx.fill();
    for(let layer=0;layer<7;layer++) {
      const ybase=.52+layer*.069,amplitude=.11+layer*.006,phase=layer*1.17+t*(.012+layer*.004);
      const g=ctx.createLinearGradient(0,h*.3,0,h);g.addColorStop(0,`rgba(39,79,90,${.075+layer*.038})`);g.addColorStop(1,`rgba(28,60,74,${.13+layer*.044})`);ctx.fillStyle=g;ctx.beginPath();ctx.moveTo(0,h);
      for(let x=0;x<=w+5;x+=mini?3:5)ctx.lineTo(x,h*(ybase-mountain(x/w,phase,layer)*amplitude));
      ctx.lineTo(w,h);ctx.closePath();ctx.fill();
    }
    const mist=ctx.createLinearGradient(0,h*.45,0,h*.92);mist.addColorStop(0,'#dfe9e700');mist.addColorStop(.43,'#dfe9e769');mist.addColorStop(1,'#dfe9e700');ctx.fillStyle=mist;ctx.fillRect(0,0,w,h);
    if(!mini)for(let i=0;i<4;i++) {
      const x=w*(.59+i*.03)+Math.sin(t*.07+i)*w*.015,y=h*(.38+i*.012)+Math.sin(t*.4+i)*2,s=2.5-i*.35;
      ctx.strokeStyle='#324f5d6b';ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(x-s*2,y-s*.25);ctx.quadraticCurveTo(x-s,y-s,x,y);ctx.quadraticCurveTo(x+s,y-s*.7,x+s*2,y-s*.15);ctx.stroke();
    }
  }
  function bloom(ctx,w,h,t,mini,surface) {
    const u=Math.min(w,h),cx=w*(mini ? .5 : .66),cy=h*.53,radius=u*.35;
    backdrop(ctx,w,h,'#0b1723','#19343f',.66,.53);stars(ctx,w,h,t,.085);
    ctx.fillStyle=radial(ctx,cx,cy,radius*1.2,[[0,'#b0e4da13'],[.6,'#78aea20b'],[1,'#78aea200']]);ctx.fillRect(0,0,w,h);
    // Open petal paths, with room between them: no filled leaves or dense seed disk.
    const petals=mini?12:16,breath=1+Math.sin(t*.34)*.055;
    const interaction=surface?.interaction;
    const pointer=interaction?.strength>.005?{x:interaction.x*w,y:interaction.y*h,strength:interaction.strength,reachSquared:(u*.29)**2}:null;
    // Values shared by thousands of samples are evaluated once per frame.
    const ripples=(interaction?.ripples||[]).map(ripple=>({x:ripple.x*w,y:ripple.y*h,
      ring:u*(.025+ripple.age*.28),band:u*.055,push:Math.sin(Math.PI*ripple.age/1.8)*u*.024}));
    const petalFrames=Array.from({length:petals},(_,petal)=>petalFrame(petal));
    function petalFrame(petal){
      const phi=petal/petals*TAU+t*.018;
      return {phi,scale:radius*breath*(1+.025*Math.sin(phi*3+t*.23)),spread:.56+.08*Math.sin(t*.15+phi)};
    }
    function point(petal,progress,offset=0) {
      const data=petalFrames[petal]||petalFrame(petal),phi=data.phi;
      const opening=Math.pow(Math.max(0,Math.sin(progress*Math.PI)),.84);
      const r=(.12+opening*.87)*data.scale+offset;
      const angle=phi+(progress-.5)*data.spread;
      const lift=Math.sin(progress*TAU+phi)*opening*radius*.11;
      let x=cx+Math.cos(angle)*r,y=cy+Math.sin(angle)*r*.9+lift;
      if(!mini&&pointer) {
        const dx=x-pointer.x,dy=y-pointer.y;
        const influence=Math.exp(-(dx*dx+dy*dy)/pointer.reachSquared)*pointer.strength;
        x+=dx*.18*influence;y+=dy*.18*influence;
      }
      if(!mini)for(const ripple of ripples) {
          const rx=x-ripple.x,ry=y-ripple.y,d=Math.sqrt(rx*rx+ry*ry),delta=(d-ripple.ring)/ripple.band;
          const push=Math.exp(-delta*delta)*ripple.push;
          x+=rx/Math.max(1,d)*push;y+=ry/Math.max(1,d)*push;
      }
      return {x,y,depth:(Math.sin(angle)+1)*.5};
    }
    ctx.save();ctx.globalCompositeOperation='screen';
    if(state.bloomVariant==='lines') {
      const strands=mini?1:3,steps=mini?40:88;
      const cacheKey=`${w}:${h}`;
      if(surface.bloomGradientKey!==cacheKey){
        surface.bloomGradientKey=cacheKey;
        surface.bloomGradients=Array.from({length:6},(_,i)=>{
          const gradient=ctx.createLinearGradient(cx-radius,cy-radius,cx+radius,cy+radius);
          gradient.addColorStop(0,'#adddd938');gradient.addColorStop(.45,i%3===1?'#e2eee4b0':'#b1d4d34c');
          gradient.addColorStop(1,i<3?'#dfcda855':'#97b9d438');return gradient;
        });
      }
      for(let petal=0;petal<petals;petal++)for(let strand=0;strand<strands;strand++) {
        ctx.strokeStyle=surface.bloomGradients[(petal%4===0?0:3)+strand];ctx.lineWidth=mini?.55:strand===1?.85:.55;ctx.beginPath();
        for(let step=0;step<=steps;step++) {
          const p=point(petal,step/steps,(strand-(strands-1)/2)*u*.003);
          if(step===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);
        }ctx.stroke();
      }
      // Sparse glints travel along the filaments, rather than blinking in place.
      for(let i=0;i<(mini?10:32);i++) {
        const glint=bloomGlints[i],p=point(i%petals,(glint.progress+t*glint.speed)%1);
        const size=mini?.6:.8+p.depth*.6;
        ctx.fillStyle=i%5===0?'#e8d7b6d0':'#d8f2edb0';ctx.beginPath();ctx.arc(p.x,p.y,size,0,TAU);ctx.fill();
        if(!mini&&i%5===0){ctx.fillStyle=radial(ctx,p.x,p.y,7,[[0,'#d9efe93d'],[1,'#d9efe900']]);ctx.fillRect(p.x-7,p.y-7,14,14);}
      }
    }else{
      const count=mini?180:900;
      for(let i=0;i<count;i++) {
        const particle=bloomParticles[i],progress=(particle.progress+t*particle.speed)%1;
        const p=point(i%petals+particle.petalOffset,progress,particle.offset*u*.025);
        const shimmer=.7+.3*Math.sin(t*.4+particle.phase),alpha=particle.alpha*shimmer;
        ctx.fillStyle=`rgba(${i%7===0?'233,216,183':i%3===0?'160,192,217':'199,232,223'},${alpha})`;
        ctx.beginPath();ctx.arc(p.x,p.y,(mini?.35:particle.size)*(1+p.depth*.2),0,TAU);ctx.fill();
        if(!mini&&i%113===0){ctx.strokeStyle=`rgba(221,243,232,${alpha*.4})`;ctx.lineWidth=.5;ctx.beginPath();ctx.moveTo(p.x-3,p.y);ctx.lineTo(p.x+3,p.y);ctx.moveTo(p.x,p.y-3);ctx.lineTo(p.x,p.y+3);ctx.stroke();}
      }
    }
    if(!mini&&surface?.interaction) {
      const interaction=surface.interaction;
      if(interaction.strength>.005) {
        const x=interaction.x*w,y=interaction.y*h,r=u*.12;
        ctx.fillStyle=radial(ctx,x,y,r,[[0,`rgba(184,233,218,${interaction.strength*.09})`],[1,'#b8e9da00']]);
        ctx.fillRect(x-r,y-r,r*2,r*2);
      }
      for(const ripple of interaction.ripples) {
        const progress=ripple.age/1.8,fade=Math.pow(1-progress,2),x=ripple.x*w,y=ripple.y*h;
        const r=u*(.025+ripple.age*.28);
        ctx.strokeStyle=`rgba(177,226,215,${fade*.26})`;ctx.lineWidth=.8;
        ctx.beginPath();ctx.arc(x,y,r,0,TAU);ctx.stroke();
        for(let i=0;i<18;i++) {
          const angle=seed(i+4000)*TAU,d=r*(.65+seed(i+4200)*.4);
          ctx.fillStyle=`rgba(232,225,199,${fade*(.25+seed(i+4400)*.4)})`;
          ctx.beginPath();ctx.arc(x+Math.cos(angle)*d,y+Math.sin(angle)*d,.6+seed(i+4600)*.7,0,TAU);ctx.fill();
        }
      }
    }
    ctx.restore();
  }
  function matrix(ctx,w,h,t,mini) {
    backdrop(ctx,w,h,'#141922','#28303e',.67,.64);
    const u=Math.min(w,h),size=u*((mini ? 0.075 : 0.06)),cx=w*((mini ? 0.5 : 0.67)),cy=h*.27;
    const count=mini?6:9;
    for(let row=0;row<count;row++)for(let col=0;col<count;col++) {
      const d=Math.hypot(row-count*.5,col-count*.5),wave=Math.sin(d*.95-t*.5);
      const x=cx+(col-row)*size,y=cy+(col+row)*size*.48,z=(.24+(wave+1)*.5)*size;
      const hue=215+Math.sin(row+col+t*.08)*10;
      const warm=Math.sin((row+col)*.7-t*.2)>.8;
      const top=warm?`hsla(31,37%,${54+wave*9}%,.9)`:`hsla(${hue},18%,${40+wave*12}%,.9)`;
      ctx.beginPath();ctx.moveTo(x-size*.89,y);ctx.lineTo(x,y+size*.43);ctx.lineTo(x,y+size*.43+z);ctx.lineTo(x-size*.89,y+z);ctx.closePath();ctx.fillStyle='#283343';ctx.fill();
      ctx.beginPath();ctx.moveTo(x,y+size*.43);ctx.lineTo(x+size*.89,y);ctx.lineTo(x+size*.89,y+z);ctx.lineTo(x,y+size*.43+z);ctx.closePath();ctx.fillStyle=warm?'#665e54':'#394452';ctx.fill();
      ctx.beginPath();ctx.moveTo(x-size*.89,y);ctx.lineTo(x,y-size*.43);ctx.lineTo(x+size*.89,y);ctx.lineTo(x,y+size*.43);ctx.closePath();ctx.fillStyle=top;ctx.fill();
      ctx.strokeStyle='#ffffff18';ctx.lineWidth=.5;ctx.stroke();
    }
  }
  const painters={orbit,aurora,ink,bloom,matrix};

  function register(canvas,scene,mini=false) {
    const surface={canvas,scene,mini,ctx:canvas.getContext('2d'),w:0,h:0,dpr:1,visible:true};
    if(!surface.ctx)return;
    surfaces.push(surface);
    const resize=()=>{
      const bounds=canvas.getBoundingClientRect();
      surface.w=bounds.width;surface.h=bounds.height;surface.dpr=Math.min(devicePixelRatio||1,2);
      canvas.width=Math.round(bounds.width*surface.dpr);canvas.height=Math.round(bounds.height*surface.dpr);
      paint(surface);
    };
    new ResizeObserver(resize).observe(canvas);
    if('IntersectionObserver' in window)new IntersectionObserver(entries=>{
      surface.visible=entries[0].isIntersecting;if(surface.visible)paint(surface);
    }).observe(canvas);
    resize();
  }
  function paint(surface) {
    if(!surface.ctx||!surface.w||!surface.h)return;
    const id=typeof surface.scene==='function'?surface.scene():surface.scene;
    const ctx=surface.ctx;ctx.save();ctx.setTransform(surface.dpr,0,0,surface.dpr,0,0);
    painters[id](ctx,surface.w,surface.h,state.time,surface.mini,surface);ctx.restore();
  }
  function clearInteractions(){for(const surface of surfaces)if(surface.interaction){surface.interaction.strength=0;surface.interaction.active=false;surface.interaction.ripples=[];}}
  function updateInteractions(dt){
    for(const surface of surfaces)if(surface.interaction){
      const interaction=surface.interaction,blend=1-Math.exp(-dt*8);
      interaction.x+=(interaction.targetX-interaction.x)*blend;interaction.y+=(interaction.targetY-interaction.y)*blend;
      interaction.strength+=((interaction.active?1:0)-interaction.strength)*blend;
      for(const ripple of interaction.ripples)ripple.age+=dt;
      interaction.ripples=interaction.ripples.filter(ripple=>ripple.age<1.8);
    }
  }
  function addRipple(surface,x,y){
    if(state.scene!=='bloom'||state.paused||state.reduced||document.hidden)return;
    const ripples=surface.interaction.ripples;
    if(ripples.length>=4)ripples.shift();
    ripples.push({x,y,age:0});
  }
  function bindGardenInteraction(canvas,host){
    const surface=surfaces.find(surface=>surface.canvas===canvas);
    surface.interaction={x:.66,y:.53,targetX:.66,targetY:.53,strength:0,active:false,ripples:[]};
    function locate(event){
      if(state.scene!=='bloom'||state.paused||state.reduced||event.isPrimary===false||event.target.closest('button')){surface.interaction.active=false;return null;}
      const rect=host.getBoundingClientRect();
      const x=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
      const y=Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height));
      Object.assign(surface.interaction,{targetX:x,targetY:y,active:true});return {x,y};
    }
    host.addEventListener('pointermove',locate,{passive:true});
    host.addEventListener('pointerdown',event=>{if(event.button!==0)return;const p=locate(event);if(p)addRipple(surface,p.x,p.y);},{passive:true});
    for(const name of ['pointerleave','pointercancel'])host.addEventListener(name,()=>surface.interaction.active=false);
    host.addEventListener('pointerup',event=>{if(event.pointerType!=='mouse')surface.interaction.active=false;});
    return surface;
  }
  function repaint(){for(const surface of surfaces)if(surface.visible)paint(surface);}
  function animate(timestamp) {
    frame=0;
    if(document.hidden)return;
    frameBudget+=Math.min(timestamp-lastFrame,80);lastFrame=timestamp;
    // Keep fractional time instead of resetting the deadline: 33.2ms must not
    // miss a 33.3ms gate and turn a nominal 30 FPS loop into uneven 20–30 FPS.
    if(!state.paused&&!state.reduced&&frameBudget>=1000/60-.5){
      frameBudget=Math.max(0,frameBudget-1000/60);
      const dt=Math.min((timestamp-lastPaint)/1000,.08);lastPaint=timestamp;
      state.time+=dt*(.35+state.intensity*1.2);updateInteractions(dt);
      const drawThumbs=timestamp-lastThumbPaint>=1000/12-.5;
      for(const surface of surfaces)if(surface.visible&&(!state.covered||surface.canvas.id==='fullCanvas')&&(!surface.mini||drawThumbs))paint(surface);
      if(drawThumbs)lastThumbPaint=timestamp;
    }
    if(!state.paused&&!state.reduced)frame=requestAnimationFrame(animate);
  }
  function syncLoop(){
    if(frame)cancelAnimationFrame(frame);frame=0;lastFrame=lastPaint=performance.now();frameBudget=0;lastThumbPaint=-Infinity;
    if(state.paused||state.reduced||document.hidden)clearInteractions();
    $('bloomRippleButton').disabled=state.paused||state.reduced;
    document.querySelector('.stage-mode').textContent=state.reduced?'减少动态 · 静态呈现':state.paused?'动效已暂停':'实时动效';
    repaint();if(!document.hidden&&!state.paused&&!state.reduced)frame=requestAnimationFrame(animate);
  }

  function selectScene(id,announce=false) {
    const scene=scenes.find(s=>s.id===id);if(!scene)return;
    clearInteractions();
    state.scene=id;$('stage').dataset.theme=id;$('standbyLayer').dataset.theme=id;
    $('sceneName').textContent=scene.name;$('sceneDescription').textContent=scene.description;
    $('stagePoem').textContent=scene.poem;$('fullPoem').textContent=scene.poem;$('fullSceneName').textContent=scene.name;
    $('bloomVariants').hidden=id!=='bloom';syncBloomVariant();
    $('bloomInteraction').hidden=id!=='bloom';
    document.querySelector('.full-quiet').textContent=id==='bloom'?'移动轻拂 · 轻点涟漪 · R 唤起':'离开一会儿，也很好。';
    document.querySelectorAll('.scene-option').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.scene===id)));
    try{localStorage.setItem('m-agent.standby-lab.scene',id);}catch{/* Preview remains usable without storage. */}
    repaint();if(announce)toast(`正在体验「${scene.name}」`);
  }
  function cycleScene(direction){const i=scenes.findIndex(scene=>scene.id===state.scene);selectScene(scenes[(i+direction+scenes.length)%scenes.length].id);}
  function syncBloomVariant(){
    document.querySelectorAll('[data-bloom]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.bloom===state.bloomVariant)));
    if(state.scene==='bloom')$('sceneDescription').textContent=state.bloomVariant==='lines'?'细线缓缓舒展，微光沿着花冠轻轻游走。':'细小的星尘汇成花的轮廓，在呼吸间散开、聚拢。';
  }
  function toast(message){clearTimeout(toastTimeout);$('toast').textContent=message;$('toast').hidden=false;toastTimeout=setTimeout(()=>$('toast').hidden=true,3500);}
  function clocks(){
    const now=new Date();const time=now.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
    const date=now.toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
    document.querySelectorAll('.time-display').forEach(el=>{el.textContent=time;el.dateTime=now.toISOString();});
    document.querySelectorAll('.date-display').forEach(el=>el.textContent=date);
  }
  async function cover(nativeFullscreen=false) {
    if(state.covered)return;
    cancelSimulation();clearInteractions();previousFocus=document.activeElement;state.covered=true;
    $('toast').hidden=true;$('standbyLayer').hidden=false;$('labApp').inert=true;$('labApp').setAttribute('aria-hidden','true');
    document.body.style.overflow='hidden';clocks();repaint();$('returnButton').focus();
    if(nativeFullscreen&&$('standbyLayer').requestFullscreen){try{await $('standbyLayer').requestFullscreen();}catch{/* Opaque window overlay remains in place. */}}
  }
  async function uncover() {
    if(!state.covered)return;
    endHold();clearInteractions();
    if(document.fullscreenElement){try{await document.exitFullscreen();}catch{/* Do not make restoration depend on browser fullscreen support. */}}
    state.covered=false;$('standbyLayer').hidden=true;$('labApp').inert=false;$('labApp').removeAttribute('aria-hidden');
    document.body.style.overflow='';state.lastActivity=Date.now();syncLoop();
    if(previousFocus?.isConnected)previousFocus.focus();else $('experienceButton').focus();
  }
  function startHold(event) {
    if(event.button!==undefined&&event.button!==0)return;
    event.preventDefault();endHold();holdStarted=performance.now();
    if(event.pointerId!==undefined)$('returnButton').setPointerCapture?.(event.pointerId);
    const tick=now=>{
      const progress=Math.min(1,(now-holdStarted)/650);$('holdProgress').style.transform=`scaleX(${progress})`;
      if(progress>=1){uncover();return;}holdFrame=requestAnimationFrame(tick);
    };holdFrame=requestAnimationFrame(tick);
  }
  function endHold(){if(holdFrame)cancelAnimationFrame(holdFrame);holdFrame=0;holdStarted=0;$('holdProgress').style.transform='scaleX(0)';}
  function cancelSimulation(){state.simulation=null;$('simulation').hidden=true;}
  function activity(){if(!state.covered)state.lastActivity=Date.now();}

  for(const scene of scenes) {
    const button=document.createElement('button');button.className='scene-option';button.type='button';button.dataset.scene=scene.id;
    button.setAttribute('aria-pressed',String(scene.id===state.scene));button.setAttribute('aria-label',scene.name+'，'+scene.note);
    const thumb=document.createElement('span');thumb.className='thumb-wrap';const canvas=document.createElement('canvas');canvas.setAttribute('aria-hidden','true');thumb.append(canvas);
    const copy=document.createElement('span');copy.className='option-copy';const name=document.createElement('span');name.className='option-name';name.textContent=scene.name;
    if(scene.tag){const tag=document.createElement('span');tag.className='option-tag';tag.textContent=scene.tag;name.append(tag);}
    const note=document.createElement('span');note.className='option-note';note.textContent=scene.note;copy.append(name,note);
    const dot=document.createElement('span');dot.className='selected-dot';dot.setAttribute('aria-hidden','true');button.append(thumb,copy,dot);
    button.addEventListener('click',()=>selectScene(scene.id));$('sceneList').append(button);register(canvas,scene.id,true);
  }
  register($('stageCanvas'),()=>state.scene);register($('fullCanvas'),()=>state.scene);
  const stageGarden=bindGardenInteraction($('stageCanvas'),$('stage'));
  const fullGarden=bindGardenInteraction($('fullCanvas'),$('standbyLayer'));
  $('bloomRippleButton').addEventListener('click',()=>addRipple(stageGarden,.66,.53));
  document.querySelectorAll('[data-bloom]').forEach(button=>button.addEventListener('click',()=>{
    state.bloomVariant=button.dataset.bloom;
    try{localStorage.setItem('m-agent.standby-lab.bloom',state.bloomVariant);}catch{/* Optional local preview preference. */}
    syncBloomVariant();repaint();
  }));
  $('experienceButton').addEventListener('click',()=>cover(true));
  $('returnButton').addEventListener('pointerdown',startHold);
  $('returnButton').addEventListener('pointermove',event=>{
    if(!holdStarted)return;
    const bounds=$('returnButton').getBoundingClientRect();
    if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)endHold();
  });
  for(const event of ['pointerup','pointercancel','lostpointercapture'])$('returnButton').addEventListener(event,endHold);
  $('returnButton').addEventListener('keydown',event=>{
    if(event.key==='Enter'||event.key===' '){event.preventDefault();uncover();}
  });
  $('returnButton').addEventListener('click',event=>{if(event.detail===0)uncover();});
  $('previousScene').addEventListener('click',()=>cycleScene(-1));$('nextScene').addEventListener('click',()=>cycleScene(1));
  document.addEventListener('keydown',event=>{
    if(!state.covered)return;
    if(event.key.toLowerCase()==='r'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&state.scene==='bloom'){
      event.preventDefault();if(!event.repeat)addRipple(fullGarden,.66,.53);
    }
    if(event.key==='ArrowLeft'){event.preventDefault();cycleScene(-1);}
    if(event.key==='ArrowRight'){event.preventDefault();cycleScene(1);}
    if(event.key==='Tab'){
      const items=[$('previousScene'),$('nextScene'),$('returnButton')],i=items.indexOf(document.activeElement);
      event.preventDefault();items[(i+(event.shiftKey?-1:1)+items.length)%items.length].focus();
    }
    // Escape may exit native fullscreen, but must never reveal the underlying content.
  });
  $('simulateButton').addEventListener('click',()=>{
    state.simulation=Date.now()+5000;$('simulationText').textContent='5 秒后进入待机';$('simulation').hidden=false;
  });$('cancelSimulation').addEventListener('click',cancelSimulation);
  $('idleSelect').addEventListener('change',event=>{state.idleSeconds=Number(event.target.value);state.lastActivity=Date.now();});
  $('intensityRange').addEventListener('input',event=>{state.intensity=Number(event.target.value)/100;$('intensityValue').value=state.intensity<.45?'轻柔':state.intensity<.8?'舒缓':'生动';repaint();});
  $('brightnessRange').addEventListener('input',event=>{state.brightness=Number(event.target.value)/100;document.documentElement.style.setProperty('--art-brightness',String(state.brightness));$('brightnessValue').value=event.target.value+'%';});
  $('clockToggle').addEventListener('change',event=>document.body.classList.toggle('clock-hidden',!event.target.checked));
  $('reducedToggle').checked=state.reduced;
  $('reducedToggle').addEventListener('change',event=>{state.reduced=event.target.checked;syncLoop();});
  media.addEventListener('change',event=>{state.reduced=event.matches;$('reducedToggle').checked=event.matches;syncLoop();});
  $('pauseButton').addEventListener('click',()=>{
    state.paused=!state.paused;document.body.classList.toggle('paused',state.paused);
    $('pauseButton').setAttribute('aria-pressed',String(state.paused));$('pauseText').textContent=state.paused?'继续动效':'暂停动效';$('pauseGlyph').textContent=state.paused?'▷':'Ⅱ';syncLoop();
  });
  $('chooseButton').addEventListener('click',()=>{
    const scene=scenes.find(s=>s.id===state.scene);try{localStorage.setItem('m-agent.standby-lab.choice',state.scene);}catch{/* Preference storage is optional. */}
    const label=scene.name+(state.scene==='bloom'?' / '+(state.bloomVariant==='lines'?'流线花冠':'星尘花园'):'');
    $('preferenceTitle').textContent=`已记下：${label}`;toast(`已记下「${label}」，仅保存本机预览偏好`);
  });
  for(const event of ['pointermove','pointerdown','keydown','wheel','touchstart'])document.addEventListener(event,activity,{passive:true});
  document.addEventListener('visibilitychange',syncLoop);
  setInterval(()=>{
    if(state.covered)return;
    const now=Date.now();
    if(state.simulation){
      const seconds=Math.ceil((state.simulation-now)/1000);
      if(seconds<=0)cover();else $('simulationText').textContent=`${seconds} 秒后进入待机`;
    }else if(state.idleSeconds>0&&now-state.lastActivity>=state.idleSeconds*1000)cover();
  },250);
  clocks();setInterval(clocks,10000);
  try{const saved=localStorage.getItem('m-agent.standby-lab.scene');if(scenes.some(s=>s.id===saved))state.scene=saved;}catch{/* Use the recommended scene. */}
  try{const saved=localStorage.getItem('m-agent.standby-lab.bloom');if(['lines','particles'].includes(saved))state.bloomVariant=saved;}catch{/* Default to fine lines. */}
  const query=new URLSearchParams(location.search);
  if(scenes.some(scene=>scene.id===query.get('scene')))state.scene=query.get('scene');
  if(['lines','particles'].includes(query.get('bloom')))state.bloomVariant=query.get('bloom');
  selectScene(state.scene);syncLoop();
})();
