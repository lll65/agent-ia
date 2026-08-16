import { chromium } from 'playwright';
const b=await chromium.launch({executablePath:'/opt/pw-browsers/chromium',args:['--headless=new','--no-sandbox']});
const p=await b.newPage({viewport:{width:390,height:720},deviceScaleFactor:2,isMobile:true,hasTouch:true});
const errs=[]; p.on('pageerror',e=>errs.push(e.message));
await p.goto('http://127.0.0.1:8931/nova',{waitUntil:'domcontentloaded'});
await p.evaluate(()=>{ localStorage.setItem('nova_key','x'); });
await p.reload({waitUntil:'domcontentloaded'}); await p.waitForTimeout(900);
await p.evaluate(()=>{ const m=document.getElementById('cfg'); if(m) m.classList.remove('show'); });
// chips sur une seule rangée ?
const chips=await p.evaluate(()=>{ const c=document.querySelector('.chips');
  const rows=new Set([...c.children].map(e=>Math.round(e.getBoundingClientRect().top)));
  return {rows:rows.size, h:Math.round(c.getBoundingClientRect().height)}; });
console.log('suggestions : ', chips.rows, 'rangée(s), hauteur', chips.h+'px', chips.rows===1?'✓':'✗');
await p.screenshot({path:'mob1.png'});
// mode vocal
await p.evaluate(()=>{ voiceMode=false; toggleVoiceMode(); });
await p.waitForTimeout(700);
const vis=await p.evaluate(()=>{ const h=getComputedStyle(document.querySelector('header')).display;
  const o=document.querySelector('#voiceUI .orb').getBoundingClientRect();
  const w=document.querySelector('#voiceUI .vwrap').getBoundingClientRect();
  return {header:h, orbBottom:Math.round(o.bottom), wrapTop:Math.round(w.top)}; });
console.log('header en vocal :', vis.header, vis.header==='none'?'✓':'✗');
console.log('orbe/texte : orbe finit à', vis.orbBottom, '· texte commence à', vis.wrapTop, vis.wrapTop>=vis.orbBottom?'✓ pas de chevauchement':'✗');
await p.screenshot({path:'mob2.png'});
console.log('erreurs :', errs.length?errs.join(' | '):'aucune ✓');
await b.close();
