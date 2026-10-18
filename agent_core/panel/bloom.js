/* Shared production painter, based on the selected standby-lab garden. No application data. */
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

export function createBloomPainter(getVariant) {
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
    if(getVariant()==='lines') {
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
  return bloom;
}
