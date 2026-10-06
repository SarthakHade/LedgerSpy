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
   Tab Switching (content is rendered by js/risk-ui.js from API data)
   ============================================================ */
function switchTab(id, btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
  if(id === 'tab-ex'){
    setTimeout(()=>{ document.querySelectorAll('.fac-bar-fill').forEach(b=>{ if(b.dataset.w) b.style.width=b.dataset.w+'%'; }); }, 100);
  }
}
