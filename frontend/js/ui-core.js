/* ============================================================
   UI Core - Cursors & Navigation
   ============================================================ */
const cursor=document.getElementById('cursor'),ring=document.getElementById('cursor-ring');
let mx=0,my=0,rx=0,ry=0;
document.addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;cursor.style.left=mx+'px';cursor.style.top=my+'px'});
function animRing(){rx+=(mx-rx)*0.12;ry+=(my-ry)*0.12;ring.style.left=rx+'px';ring.style.top=ry+'px';requestAnimationFrame(animRing)}
animRing();

/* ============================================================
   Background Hex Canvas Animation (decorative; deterministic phases)
   ============================================================ */
(function(){
  const c=document.getElementById('bg-canvas');
  if(!c) return;
  const ctx=c.getContext('2d');
  let W,H,hexes=[]; const S=40,R=S/Math.sqrt(3);
  function init(){
    W=c.width=window.innerWidth; H=c.height=window.innerHeight; hexes=[];
    const colW=S*1.5+2, rowH=R*2;
    let k=0;
    for(let r=0;r<Math.ceil(H/rowH)+2;r++){
      for(let cc=0;cc<Math.ceil(W/colW)+2;cc++){
        hexes.push({x:cc*(S*1.5+2)+(r%2?S*0.75:0), y:r*R, phase:lsHash01(k)*Math.PI*2, speed:0.2+lsHash01(k+7919)*0.3});
        k++;
      }
    }
  }
  function draw(){
    ctx.clearRect(0,0,W,H);
    let t=Date.now()*0.001;
    ctx.lineWidth=0.5;
    hexes.forEach(h=>{
      let b = 0.2 + 0.8*Math.abs(Math.sin(t*h.speed+h.phase));
      ctx.strokeStyle=`rgba(34,211,238,${0.02 + b*0.06})`;
      ctx.beginPath();
      for(let i=0;i<6;i++){
        let a = i*Math.PI/3 - Math.PI/6;
        ctx.lineTo(h.x + (R-2)*Math.cos(a), h.y + (R-2)*Math.sin(a));
      }
      ctx.closePath(); ctx.stroke();
    });
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize',init); init(); draw();
})();

/* ============================================================
   Tab Switching & UI Initializations
   ============================================================ */
function switchTab(id, btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
  if(id === 'tab-an' && !window._scatterBuilt){
    window._scatterBuilt = true;
    setTimeout(buildScatter, 50);
  }
  if(id === 'tab-ex' && !window._barsAnimated){
    window._barsAnimated = true;
    setTimeout(()=>{
      document.querySelectorAll('.fac-bar-fill').forEach(b=>b.style.width=b.dataset.w+'%');
    }, 100);
  }
}

/* ============================================================
   Scatter: heuristic score (x) vs amount (y, log scale).
   Deterministic: no random jitter or sampling.
   ============================================================ */
function buildScatter(){
  const SW = 760, SH = 310, PAD = 40;
  const svg = document.getElementById('scatter-svg');
  if(!svg) return;
  svg.querySelectorAll('.scatter-dot').forEach(d => d.remove());
  const results = window._mlResults;
  if(!results || results.length === 0) return;

  const logA = a => Math.log10(Math.max(1, Math.abs(a || 0)));
  let maxLog = 1;
  results.forEach(r => { const l = logA(r.amount); if(l > maxLog) maxLog = l; });

  // Large files: keep every high/suspicious row; thin "below threshold" rows by a fixed stride.
  const MAX_DOTS = 1500;
  const flaggedCount = results.filter(r => r.mlResult.tier !== 'safe').length;
  const safeTotal = results.length - flaggedCount;
  const safeQuota = Math.max(0, MAX_DOTS - flaggedCount);
  const stride = safeTotal > safeQuota && safeQuota > 0 ? Math.ceil(safeTotal / safeQuota) : 1;
  let safeSeen = 0;

  const ns = 'http://www.w3.org/2000/svg';
  results.forEach((d, idx) => {
    const tier = d.mlResult.tier;
    if(tier === 'safe'){
      const keep = (safeSeen % stride) === 0;
      safeSeen++;
      if(!keep || safeQuota === 0) return;
    }
    const jx = (lsHash01(idx) - 0.5) * 6;          // small deterministic jitter to separate ties
    const cx = Math.max(PAD, Math.min(SW, PAD + 10 + (d.mlResult.riskScore / 100) * (SW - PAD - 20) + jx));
    const cy = Math.max(10, Math.min(SH, SH - 10 - (logA(d.amount) / maxLog) * (SH - 30)));

    const c = document.createElementNS(ns, 'circle');
    c.setAttribute('cx', cx.toFixed(1));
    c.setAttribute('cy', cy.toFixed(1));
    c.setAttribute('r', tier === 'high' ? 5 : tier === 'suspicious' ? 4 : 3);
    c.setAttribute('fill', tier === 'high' ? 'var(--red)' : tier === 'suspicious' ? 'var(--amber)' : 'var(--cyan)');
    c.setAttribute('opacity', tier === 'safe' ? '0.35' : '0.85');
    c.setAttribute('class', 'scatter-dot');

    c.addEventListener('mouseover', e => {
      const tt  = document.getElementById('ano-tt');
      const box = document.getElementById('scatter-box').getBoundingClientRect();
      tt.textContent = (d._displayId || '') + ' — ' + d.mlResult.decision + ' — score ' + d.mlResult.riskScore + '/100';
      tt.style.opacity = '1';
      tt.style.left = (e.clientX - box.left + 14) + 'px';
      tt.style.top  = (e.clientY - box.top  - 36) + 'px';
    });
    c.addEventListener('mouseout', () => {
      const tt = document.getElementById('ano-tt');
      if(tt) tt.style.opacity = '0';
    });
    c.addEventListener('click', () => {
      const panel = document.getElementById('ano-panel');
      if(panel) panel.style.display = 'block';
      setText('ap-label', tier === 'high' ? 'HIGH RISK (HEURISTIC): ' : tier === 'suspicious' ? 'SUSPICIOUS (HEURISTIC): ' : 'BELOW THRESHOLDS: ');
      setText('ap-id', d._displayId || '');
      setText('ap-v', d.nameDest || 'Unknown payee');
      setText('ap-amt', '₹' + Math.floor(d.amount || 0).toLocaleString() + ' (score ' + d.mlResult.riskScore + '/100)');
      setText('ap-rsn', d.mlResult.reasons.join(' | '));
    });
    svg.appendChild(c);
  });
}
