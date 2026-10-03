/* ============================================================
   UI Core - Cursors & Navigation
   ============================================================ */
const cursor=document.getElementById('cursor'),ring=document.getElementById('cursor-ring');
let mx=0,my=0,rx=0,ry=0;
document.addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;cursor.style.left=mx+'px';cursor.style.top=my+'px'});
function animRing(){rx+=(mx-rx)*0.12;ry+=(my-ry)*0.12;ring.style.left=rx+'px';ring.style.top=ry+'px';requestAnimationFrame(animRing)}
animRing();

/* ============================================================
   Background Hex Canvas Animation
   ============================================================ */
(function(){
  const c=document.getElementById('bg-canvas');
  if(!c) return;
  const ctx=c.getContext('2d');
  let W,H,hexes=[]; const S=40,R=S/Math.sqrt(3);
  function init(){
    W=c.width=window.innerWidth; H=c.height=window.innerHeight; hexes=[];
    const colW=S*1.5+2, rowH=R*2;
    for(let r=0;r<Math.ceil(H/rowH)+2;r++){
      for(let cc=0;cc<Math.ceil(W/colW)+2;cc++){
        hexes.push({x:cc*(S*1.5+2)+(r%2?S*0.75:0), y:r*R, phase:Math.random()*Math.PI*2, speed:0.2+Math.random()*0.3});
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
  // Lazy-init Anomalies scatter the first time that tab is opened
  if(id === 'tab-an' && !window._scatterBuilt){
    window._scatterBuilt = true;
    setTimeout(buildScatter, 50); // tiny delay so element is painted
  }
  // Animate Explainability bars the first time that tab is opened
  if(id === 'tab-ex' && !window._barsAnimated){
    window._barsAnimated = true;
    setTimeout(()=>{
      document.querySelectorAll('.fac-bar-fill').forEach(b=>b.style.width=b.dataset.w+'%');
    }, 100);
  }
}

function buildScatter(){
  const SW = 760, SH = 310, PAD = 40;
  const svg = document.getElementById('scatter-svg');
  if(!svg) return;

  svg.querySelectorAll('.scatter-dot').forEach(d => d.remove());
  const results = window._mlResults;
  if(!results || results.length === 0) return;

  // Use actual amount field (r.amount is the direct CSV value, r.features.amount is computed)
  let maxAmt = 1, maxFreq = 1;
  results.forEach(r => {
    const amt = r.amount || r.features.amount || 0;
    if(amt > maxAmt) maxAmt = amt;
    if(r.features.transaction_count > maxFreq) maxFreq = r.features.transaction_count;
  });

  // For large datasets, sample safe transactions to avoid browser lag
  const MAX_DOTS = 800;
  const fraudCount = results.filter(r => r.mlResult.decision === 'Fraud').length;
  const suspCount  = results.filter(r => r.mlResult.decision === 'Suspicious').length;
  const safeQuota = Math.max(0, MAX_DOTS - fraudCount - suspCount);

  let safeDrawn = 0;
  results.forEach((d, idx) => {
    const isFraud = d.mlResult.decision === 'Fraud';
    const isSusp  = d.mlResult.decision === 'Suspicious';
    const isSafe  = !isFraud && !isSusp;

    // Downsample safe dots
    if(isSafe){
      if(safeDrawn >= safeQuota) return;
      if(results.length > MAX_DOTS && Math.random() > (safeQuota / (results.length - fraudCount - suspCount))) return;
      safeDrawn++;
    }

    const amt  = d.amount || d.features.amount || 0;
    const freq = d.features.transaction_count || 1;

    let cx = PAD + 20 + Math.min((freq / maxFreq) * (SW - PAD - 40), SW - PAD - 40);
    let cy = SH - 20 - Math.min((amt / maxAmt) * (SH - 40), SH - 40);

    cx += (Math.random() - 0.5) * 18;
    cy += (Math.random() - 0.5) * 18;
    cx = Math.max(PAD, Math.min(SW, cx));
    cy = Math.max(10, Math.min(SH, cy));

    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', cx);
    c.setAttribute('cy', cy);
    c.setAttribute('r', isFraud ? 6 : (isSusp ? 5 : 3));
    c.setAttribute('fill', isFraud ? 'var(--red)' : (isSusp ? 'var(--amber)' : 'var(--cyan)'));
    c.setAttribute('opacity', (isFraud || isSusp) ? '0.92' : '0.35');
    c.setAttribute('class', 'scatter-dot');
    if(isFraud) c.setAttribute('filter', 'drop-shadow(0 0 5px rgba(248,113,113,0.8))');
    else if(isSusp) c.setAttribute('filter', 'drop-shadow(0 0 3px rgba(245,158,11,0.6))');

    c.addEventListener('mouseover', e => {
      const tt  = document.getElementById('ano-tt');
      const box = document.getElementById('scatter-box').getBoundingClientRect();
      tt.textContent = isFraud
        ? `⚠ FRAUD — Risk ${d.mlResult.riskScore}%`
        : isSusp
          ? `⚠ Suspicious — Risk ${d.mlResult.riskScore}%`
          : `✓ Safe — Risk ${d.mlResult.riskScore}%`;
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
      const apId  = document.getElementById('ap-id');
      const apV   = document.getElementById('ap-v');
      const apAmt = document.getElementById('ap-amt');
      const apRsn = document.getElementById('ap-rsn');
      const txId  = (tableData[idx] && tableData[idx].id) ? tableData[idx].id : ('TX-' + (idx + 1));
      if(apId){
        apId.textContent = txId;
        apId.style.color = isFraud ? 'var(--red)' : (isSusp ? 'var(--amber)' : 'var(--cyan)');
        if(apId.previousSibling)
          apId.previousSibling.textContent = isFraud ? 'CRITICAL ALERT: ' : (isSusp ? 'WARNING: ' : 'RECORD: ');
      }
      if(apV)   apV.textContent   = d.nameDest || 'Unknown Vendor';
      if(apAmt) apAmt.textContent = '₹' + Math.floor(amt).toLocaleString() + ' (Risk: ' + d.mlResult.riskScore + '%)';
      if(apRsn) apRsn.textContent = d.mlResult.reasons[0] || 'Within normal variance bounds.';
    });
    svg.appendChild(c);
  });
}


// Tooltips for Trends
window.showTT = function(e, t){
  let tt = document.getElementById('tt-line');
  tt.textContent = "Data Segment Overlap";
  tt.style.opacity = '1';
  tt.style.left = (e.offsetX+10)+'px';
  tt.style.top = (e.offsetY+10)+'px';
}
window.hideTT = function(){
  document.getElementById('tt-line') && (document.getElementById('tt-line').style.opacity='0');
}
