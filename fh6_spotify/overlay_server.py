"""Stream overlay HTTP server -> OBS Browser Source.

Streamers add http://127.0.0.1:<port>/ as a Browser Source; OBS renders the HTML
with its OWN browser engine, so this adds no weight to Segue - we only serve a small
page + the live now-playing data on localhost. The viewers see it; the streamer's
own game view is untouched (it's an OBS source, not an in-game overlay).

Routes:
  GET /        -> the overlay HTML (transparent; polls /np, reloads /art on change)
  GET /np      -> JSON {title, artist, playing, app, ver}
  GET /art     -> current album-art bytes (image), or 204 when none
  GET /preset  -> overlay style/layout preset (JSON); the in-app visual editor writes
                  this. Clean + neutral by default; every knob is editor-driven.

Reads the shared live `ui` dict that runner.py updates. Runs in a daemon thread; all
failures are swallowed so it can never take the app down.
"""
from __future__ import annotations
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
_LOGO_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'Segue logo in bottom ui.png')
_KOFI_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'kofi_logo.png')
_DISCORD_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'Discord_logo_blue.png')
_INTER_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'Inter.ttf')
_VIZ_JS_PATH = os.path.join(os.path.dirname(__file__), 'assets', 'viz_layer.js')
BOOT_ID = str(int(time.time() * 1000))
DEFAULT_PORT = 7345
DEFAULT_PRESET = {
    'show_art': True,
    'cover_radius': 20,
    'cover_filter': 'none',
    'bg_enabled': True,
    'bg_color': '#2b2b29',
    'bg_color2': '#141414',
    'bg_opacity': 0.85,
    'bg_radius': 13,
    'cover_bg': False,
    'clip_cover': False,
    'bg_auto': False,
    'bg_grad': False,
    'bg_grad_user': False,
    'bg_grad_angle': 135,
    'accent_auto': False,
    'text_align': 'left',
    'time_align': 'left',
    'text_color': '#ffffff',
    'sub_color': '#c8c8d0',
    'accent': '#ffffff',
    'show_title': True,
    'show_artist': True,
    'show_bars': True,
    'show_prog': True,
    'show_vol': False,
    'show_time': False,
    'show_video': False,
    'video_url': '',
    'video_fit': 'cover',
    'video_radius': 0,
    'vol_style': 'arcs',
    'bars_attach': True,
    'bars_n': 5,
    'bars_gap': 0.4,
    'bars_round': True,
    'bars_glow': False,
    'bars_reactive': True,
    'show_viz': False,
    'viz_radius': 0,
    'viz': {
        'mode': 'lava',
        'auto': True,
        'speed': 1.8,
        'scale': 1.5,
        'sharp': 0.1,
        'vintage': 0.9,
        'rattle': 0.4,
        'smooth': 0.35,
        'flash': 0.1,
        'palCount': 5,
        'beatPal': 'off',
        'mono': False,
        'spectrum': False,
        'pal': ['#ff5e3a', '#ff1b6b', '#1a0b2e', '#ffc04d', '#ffe9d6'],
        'lock': [False, False, False, False, False],
    },
    'layout': {
        'cw': 300,
        'ch': 240,
        'bg': {
            'x': 0,
            'y': 0,
            'w': 252,
            'h': 84,
        },
        'cover': {
            'x': 12,
            'y': 12,
            'size': 60,
        },
        'text': {
            'x': 84,
            'y': 20,
            'scale': 1,
        },
        'bars': {
            'x': 150,
            'y': 40,
            'w': 40,
            'h': 14,
        },
        'prog': {
            'x': 84,
            'y': 74,
            'w': 150,
            'h': 4,
        },
        'vol': {
            'x': 196,
            'y': 86,
            'w': 50,
            'h': 38,
        },
        'time': {
            'x': 84,
            'y': 92,
            'w': 96,
            'h': 16,
        },
        'video': {
            'x': 0,
            'y': 0,
            'w': 252,
            'h': 84,
        },
        'viz': {
            'x': 0,
            'y': 0,
            'w': 252,
            'h': 84,
        },
        'order': ['bg', 'viz', 'video', 'cover', 'text', 'bars', 'prog', 'vol', 'time'],
    },
}
_OVERLAY_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Segue overlay</title>
<style>
  :root{ --bg:rgba(20,20,19,.85); --text:#fff; --sub:#c8c8d0; --accent:#fff; --art-radius:10px; --bg-radius:13px; }
  html,body{ margin:0; background:transparent; overflow:hidden;
    font-family:'Segoe UI',Inter,system-ui,sans-serif; }
  /* Free canvas: every element absolutely positioned from the preset layout. OBS
     positions the whole source; we just render each element at its canvas coords. */
  #wrap{ position:fixed; top:0; left:0; opacity:0; transition:opacity .3s ease, transform .3s ease; }
  #wrap.show{ opacity:1; }
  #bg{ position:absolute; background:var(--bg); border-radius:var(--bg-radius); overflow:hidden; transition:background-color .5s ease; }
  #wrap.nobg #bg{ display:none; }
  /* Optional blurred album-art card background (the track's own colours). */
  #coverbg{ position:absolute; inset:0; background-size:cover; background-position:center;
    filter:blur(16px) brightness(.55) saturate(1.3); transform:scale(1.3);
    opacity:0; transition:opacity .35s ease; }
  #coverbg.on{ opacity:1; }
  #art{ position:absolute; object-fit:cover; background:#2b2b29; border-radius:var(--art-radius); }
  #art.hidden{ display:none; } #art.noart{ object-fit:contain; }
  #meta{ position:absolute; display:flex; flex-direction:column; align-items:flex-start; }
  #title{ color:var(--text); font-weight:700; font-size:17px; line-height:1.15;
    white-space:nowrap; overflow:hidden; }
  #title.hidden,#artistname.hidden,#bars.hidden{ display:none; }
  #artist{ color:var(--sub); font-weight:500; font-size:13.5px; margin-top:3px;
    display:flex; align-items:center; gap:8px; }
  #artistname{ display:inline-block; overflow:hidden; white-space:nowrap; }
  /* On overflow: fade the right edge out instead of an ellipsis (toggled in JS). */
  .faded{ -webkit-mask-image:linear-gradient(to right,transparent 0,#000 16px,#000 calc(100% - 16px),transparent 100%);
          mask-image:linear-gradient(to right,transparent 0,#000 16px,#000 calc(100% - 16px),transparent 100%); }
  #bars{ position:absolute; } #vol{ position:absolute; } #vol.hidden{ display:none; }
  #bars.hidden{ display:none; }
  #prog{ position:absolute; height:4px; border-radius:3px; background:rgba(255,255,255,.22); overflow:hidden; }
  #time{ position:absolute; color:var(--sub); font-weight:600; font-variant-numeric:tabular-nums; white-space:nowrap; line-height:0.9; display:flex; align-items:center; }
  #vid{ position:absolute; object-fit:cover; border-radius:var(--bg-radius); display:none; }
  #time.hidden{ display:none; }
  #prog.hidden{ display:none; }
  #progfill{ height:100%; width:0%; background:var(--prog,var(--accent)); border-radius:3px; }
</style></head><body>
<div id="wrap">
  <canvas id="viz" width="252" height="84" style="position:absolute;left:0;top:0;display:none"></canvas>
  <div id="bg"><div id="coverbg"></div></div>
  <img id="art" alt="">
  <div id="meta">
    <div id="title">Nothing playing</div>
    <div id="artist"><span id="artistname"></span></div>
  </div>
  <canvas id="bars" width="23" height="11"></canvas>
  <div id="prog"><div id="progfill"></div></div>
  <canvas id="vol" width="56" height="26"></canvas>
    <div id="time">0:00 / 0:00</div>
    <video id="vid" muted loop playsinline></video>
</div>
<script src="/viz_layer.js?v=1"></script>
<script>
const R=document.documentElement, wrap=document.getElementById('wrap');
const NOART="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='%23ffffff' fill-opacity='0.28' d='M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z'/></svg>";
function hexA(hex,a){ const m=/^#?([0-9a-f]{6})$/i.exec(hex||''); if(!m) return hex||'';
  const n=parseInt(m[1],16); return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`; }
function show(id,on){ document.getElementById(id).classList.toggle('hidden', on===false); }
function pos(id,b){ const e=document.getElementById(id); if(!e||!b) return;
  if(b.x!=null) e.style.left=b.x+'px'; if(b.y!=null) e.style.top=b.y+'px';
  if(b.w!=null) e.style.width=b.w+'px'; if(b.h!=null) e.style.height=b.h+'px'; }
// When clip is on, the cover lives INSIDE #bg (clipped to the card's rounded shape);
// otherwise it's a free element on the canvas. Coords stay canvas-absolute either way.
function placeArt(clip,cov,bg){ const art=document.getElementById('art'),
    bgEl=document.getElementById('bg'), root=document.getElementById('wrap');
  if(clip){ if(art.parentNode!==bgEl) bgEl.appendChild(art);
    art.style.left=(cov.x-bg.x)+'px'; art.style.top=(cov.y-bg.y)+'px'; }
  else { if(art.parentNode!==root) root.insertBefore(art, document.getElementById('meta'));
    art.style.left=cov.x+'px'; art.style.top=cov.y+'px'; }
  art.style.width=cov.size+'px'; art.style.height=cov.size+'px'; }
function placeBars(attach){ const b=document.getElementById('bars'), ar=document.getElementById('artist'), root=document.getElementById('wrap'); const vis=_lastP.show_bars!==false;
  if(attach){ if(b.parentNode!==ar) ar.appendChild(b); b.style.position='static'; b.style.left=''; b.style.top='';
    b.style.display=vis?'inline-block':'none'; b.style.verticalAlign='middle'; b.style.width='23px'; b.style.height='11px'; }
  else { if(b.parentNode!==root) root.appendChild(b); b.style.position='absolute'; b.style.display=vis?'block':'none'; } }
// Sample the cover's dominant colour (saturation-weighted avg) for the Auto bg mode.
function sampleArt(img){ try{ const c=document.createElement('canvas'); c.width=c.height=24;
  const x=c.getContext('2d'); x.drawImage(img,0,0,24,24); const d=x.getImageData(0,0,24,24).data;
  let r=0,g=0,b=0,w=0; for(let i=0;i<d.length;i+=4){ const R=d[i],G=d[i+1],B=d[i+2];
    const wt=12+(Math.max(R,G,B)-Math.min(R,G,B)); r+=R*wt; g+=G*wt; b+=B*wt; w+=wt; }
  return [Math.round(r/w),Math.round(g/w),Math.round(b/w)]; }catch(e){ return null; } }
function bgCss(p){ const op=p.bg_opacity==null?0.85:p.bg_opacity;
  if(p.bg_grad_user){ return 'linear-gradient('+(p.bg_grad_angle==null?135:p.bg_grad_angle)+'deg, '+hexA(p.bg_color||'#2b2b29',op)+', '+hexA(p.bg_color2||'#141414',op)+')'; }   // user gradient: pick 2 colours + angle
  if(p.bg_auto && _autoRGB){ const a=_autoRGB;
    if(p.bg_grad){ const c1='rgba('+Math.round(a[0]*.72)+','+Math.round(a[1]*.72)+','+Math.round(a[2]*.72)+','+op+')',
                       c2='rgba('+Math.round(a[0]*.28)+','+Math.round(a[1]*.28)+','+Math.round(a[2]*.28)+','+op+')';
      return 'linear-gradient(135deg, '+c1+', '+c2+')'; }
    const f=0.55; return 'rgba('+Math.round(a[0]*f)+','+Math.round(a[1]*f)+','+Math.round(a[2]*f)+','+op+')'; }
  return hexA(p.bg_color||'#141414', op); }
function accentCss(p){ return (p.accent_auto && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (p.accent||'#fff'); }
function progCss(p){ return (p.prog_auto && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (p.prog_color||p.accent||'#fff'); }   // base accent, NOT accentCss -> bars auto-colour doesn't bleed into the progress bar
function volCss(p){ return p.vol_color||p.accent||'#fff'; }   // base accent, NOT accentCss -> bars auto-colour doesn't bleed into the volume meter
// Slideshow marquee: overflowing text gently glides start<->end. Edge fade is dynamic
// (fade only the side that has hidden content). Own rAF tween = slow + smooth (the native
// smooth-scroll is too snappy). Only call on text/width change (not every poll).
function updFade(c){ const over=c.scrollWidth-c.clientWidth; let m;
  if(over<=2) m='none';
  else { const l=c.scrollLeft>2?'transparent 0,#000 16px':'#000 0',
        r=c.scrollLeft<over-2?'#000 calc(100% - 16px),transparent 100%':'#000 100%';
    m='linear-gradient(to right,'+l+','+r+')'; }
  if(c._mask!==m){ c._mask=m; c.style.webkitMaskImage=m; c.style.maskImage=m; } }
function tweenScroll(c,to,dur){ if(c._tw)cancelAnimationFrame(c._tw); const from=c.scrollLeft, d=to-from; let t0=null;
  function step(ts){ if(t0===null)t0=ts; const k=Math.min(1,(ts-t0)/dur), e=k;   // linear ticker
    c.scrollLeft=from+d*e; if(k<1) c._tw=requestAnimationFrame(step); } c._tw=requestAnimationFrame(step); }
function marquee(cid){ const c=document.getElementById(cid); if(!c) return; clearInterval(c._mq);
  c.onscroll=()=>updFade(c); c.scrollLeft=0; updFade(c);
  const over=c.scrollWidth-c.clientWidth; if(over<=2) return;
  const dur=Math.min(6000,1200+over*10); let at=0;
  c._mq=setInterval(()=>{ at=1-at; tweenScroll(c, at?over:0, dur); }, dur+5000); }
const EID={bg:'bg',cover:'art',text:'meta',bars:'bars',prog:'prog',vol:'vol',time:'time',video:'vid',viz:'viz'};
function obox(part){ const L=_lastP.layout||{}; if(part==='video') return L.video||{x:0,y:0,w:0,h:0}; if(part==='viz') return L.viz||{x:0,y:0,w:0,h:0}; if(part==='bg') return L.bg||{x:0,y:0,w:0,h:0};
  if(part==='cover'){ const c=L.cover||{}; return {x:c.x,y:c.y,w:c.size,h:c.size}; }
  if(part==='prog'){ const r=L.prog||{}; return {x:r.x,y:r.y,w:r.w,h:r.h||4}; }
  if(part==='bars') return L.bars||{x:0,y:0,w:0,h:0};
  if(part==='vol') return L.vol||{x:0,y:0,w:0,h:0};
  if(part==='time'){ const t=document.getElementById('time'), b=L.time||{x:0,y:0,w:0,h:0}; if(!t) return b; const r=t.getBoundingClientRect(), w=r.width||b.w, off=_lastP.time_align==='center'?w/2:_lastP.time_align==='right'?w:0; return {x:b.x-off,y:b.y,w:w,h:r.height||b.h}; }
  const m=document.getElementById('meta').getBoundingClientRect(); return {x:(L.text||{}).x||0,y:(L.text||{}).y||0,w:m.width,h:m.height}; }
function oVisBBox(){ const parts=[];   // bounding box of the VISIBLE layers -> auto-crop the OBS output
  if(_lastP.bg_enabled!==false) parts.push('bg');
  if(_lastP.show_art!==false) parts.push('cover');
  if(_lastP.show_title!==false||_lastP.show_artist!==false) parts.push('text');
  if(_lastP.show_bars!==false) parts.push('bars');
  if(_lastP.show_prog!==false) parts.push('prog');
  if(_lastP.show_vol===true) parts.push('vol');
  if(_lastP.show_time===true) parts.push('time');
  let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
  for(const p of parts){ if(_lastP.clip&&_lastP.clip[p]) continue; const b=obox(p); if(!b||b.w==null) continue;   // clipped layers are masked within their base -> they don't extend the cropped output
    mnx=Math.min(mnx,b.x); mny=Math.min(mny,b.y); mxx=Math.max(mxx,b.x+b.w); mxy=Math.max(mxy,b.y+b.h); }
  if(mnx>mxx) return null; return {x:mnx,y:mny,w:mxx-mnx,h:mxy-mny}; }
const OBS_MARGIN=6;
function fitWrap(){ const wr=document.getElementById('wrap'); if(!wr) return;   // scale the cropped content to FILL the OBS source, whatever its size -> no exact WxH to type, ever
  wr.style.zoom=''; wr.style.transform='none'; wr.style.left='0px'; wr.style.top='0px';   // reset to measure the content UNSCALED
  const _bb=oVisBBox(); if(!_bb) return;
  const W=window.innerWidth, H=window.innerHeight, m=OBS_MARGIN*2, s=Math.max(0.05,Math.min((W-m)/Math.max(1,_bb.w),(H-m)/Math.max(1,_bb.h)));
  wr.style.zoom=s;   // ZOOM, not transform:scale -> re-rasterises text/vectors crisply at any OBS size (transform scale upscales the bitmap = blurry)
  wr.style.left=(OBS_MARGIN/s - _bb.x)+'px'; wr.style.top=(OBS_MARGIN/s - _bb.y)+'px'; }   // anchor content top-left (zoom scales left/top too)
window.addEventListener('resize', fitWrap);
function ocoverRad(p){ return p.cover_radius!=null?p.cover_radius:(p.cover_shape==='circle'?100:p.cover_shape==='sharp'?0:20); }
function oArtFilter(p){ return p.cover_filter==='grey'?'grayscale(1)':p.cover_filter==='sepia'?'grayscale(1) sepia(.7)':'none'; }
function fxFilter(pr, part){ const f=(pr.fx&&pr.fx[part])||{}; const s=[];   // per-layer FX as a composable CSS filter (outline = ring of 0-blur drop-shadows -> works on text/canvas/img)
  if(f.ol && (f.olw||0)>0 && part!=='cover' && part!=='bg' && part!=='prog' && part!=='video'){ const c=olColor(f), w=f.olw, N=8; for(let k=0;k<N;k++){ const a=k*6.2831853/N; s.push('drop-shadow('+(Math.cos(a)*w).toFixed(2)+'px '+(Math.sin(a)*w).toFixed(2)+'px 0 '+c+')'); } }   // content layers (text/bars/vol/time): light 8-dir ring. shaped layers use box-shadow (fxBoxShadow) -> cheap + perfect circle, no 24-filter crash
  if(f.sh){ const a=(f.sha==null?90:f.sha)*Math.PI/180, d=(f.shd==null?4:f.shd), op=(f.sho==null?55:f.sho)/100; s.push('drop-shadow('+(Math.cos(a)*d).toFixed(1)+'px '+(Math.sin(a)*d).toFixed(1)+'px '+(f.shb==null?6:f.shb)+'px '+hexA(f.shc||'#000000',op)+')'); }
  return s.join(' '); }
function fxBoxShadow(pr, part){ const f=(pr.fx&&pr.fx[part])||{};   // outline for SHAPED layers: one spread shadow follows border-radius (perfect circle, GPU-cheap)
  if(f.ol && (f.olw||0)>0 && (part==='cover'||part==='bg'||part==='prog'||part==='video')) return '0 0 0 '+f.olw+'px '+olColor(f);
  return ''; }
function olColor(f){ return (f.ola && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (f.olc||'#000000'); }   // outline colour: auto-sampled from cover when f.ola, else manual
function anyFxAuto(p){ if(!p||!p.fx) return false; for(const k in p.fx){ if(p.fx[k]&&p.fx[k].ola) return true; } return false; }
function applyVideo(pr, vd){ if(!vd) return;   // video layer: editor test URL (pr.video_url) or live np video; loop/mute/cover
  const vsrc=((pr.video_url||'').trim())||_npVideo||'', on=pr.show_video===true, fit=pr.video_fit||'cover', rad=(pr.video_radius||0)+'px';
  if(!on){ vd.style.display='none'; if(vd._src){ vd._src=''; vd.removeAttribute('src'); vd.load(); } vd.style.backgroundImage='none'; return; }
  vd.style.display='block'; vd.style.objectFit=fit; vd.style.borderRadius=rad;
  if(vsrc){ if(vd._src!==vsrc){ vd._src=vsrc; vd.src=vsrc; vd.play().catch(()=>{}); } vd.style.backgroundImage='none'; }
  else { if(vd._src){ vd._src=''; vd.removeAttribute('src'); vd.load(); }   // no Canvas -> album-art placeholder in the same box
    const a=document.getElementById('art'), u=a&&a.getAttribute('src'); vd.style.backgroundImage=u?('url("'+u+'")'):'none';
    vd.style.backgroundSize=(fit==='contain'?'contain':fit==='fill'?'100% 100%':'cover'); vd.style.backgroundPosition='center'; vd.style.backgroundRepeat='no-repeat'; } }
function oRadius(part,b){ if(part==='bg') return _lastP.bg_radius==null?13:_lastP.bg_radius;
  if(part==='viz') return _lastP.viz_radius||0;
  if(part==='cover') return ocoverRad(_lastP)/100*Math.min(b.w,b.h)/2; return 0; }
function orrPath(x,y,w,h,r){ const x2=x+w, y2=y+h;
  if(r<=0.5) return "path('M"+x+" "+y+" H"+x2+" V"+y2+" H"+x+" Z')";
  return "path('M"+(x+r)+" "+y+" H"+(x2-r)+" A"+r+" "+r+" 0 0 1 "+x2+" "+(y+r)+" V"+(y2-r)+" A"+r+" "+r+" 0 0 1 "+(x2-r)+" "+y2+" H"+(x+r)+" A"+r+" "+r+" 0 0 1 "+x+" "+(y2-r)+" V"+(y+r)+" A"+r+" "+r+" 0 0 1 "+(x+r)+" "+y+" Z')"; }
function oClip(part){ const E=document.getElementById(EID[part]); if(!E) return;
  const ord=(_lastP.layout&&_lastP.layout.order)||['bg','viz','video','cover','text','bars','prog','vol','time'], i=ord.indexOf(part);
  if(!(_lastP.clip&&_lastP.clip[part])){ E.style.clipPath='none'; return; }
  let bi=-1; for(let j=i-1;j>=0;j--){ if(!(_lastP.clip&&_lastP.clip[ord[j]])){ bi=j; break; } }
  if(bi<0){ E.style.clipPath='none'; return; }
  const A=obox(part), B=obox(ord[bi]); let R=oRadius(ord[bi],B); R=Math.max(0,Math.min(R,Math.min(B.w,B.h)/2));
  const sc=(part==='text')?((_lastP.layout&&_lastP.layout.text&&_lastP.layout.text.scale)||1):1;   // #meta is transform:scaled -> clip-path lives in its pre-scale local coords
  E.style.clipPath=orrPath((B.x-A.x)/sc, (B.y-A.y)/sc, B.w/sc, B.h/sc, R/sc); }
async function applyPreset(){
  try{
    const p=await (await fetch('/preset',{cache:'no-store'})).json();
    R.style.setProperty('--text', p.text_color||'#fff');
    R.style.setProperty('--sub', p.sub_color||'#c8c8d0');
    R.style.setProperty('--accent', accentCss(p));
    R.style.setProperty('--prog', progCss(p));
    R.style.setProperty('--vol', volCss(p));
    _lastP=p; if(!p.clip) p.clip = {};
    if((p.bg_auto||p.accent_auto||p.prog_auto||anyFxAuto(p)) && !_autoRGB){ const A=document.getElementById('art'); if(A.complete&&A.naturalWidth) _autoRGB=sampleArt(A); }
    R.style.setProperty('--bg', bgCss(p));
    R.style.setProperty('--bg-radius', (p.bg_radius==null?13:p.bg_radius)+'px');
    R.style.setProperty('--art-radius', (ocoverRad(p)*0.5)+'%');
    ['bg','cover','text','bars','prog','vol','time','video'].forEach(pt=>{ const e=document.getElementById({bg:'bg',cover:'art',text:'meta',bars:'bars',prog:'prog',vol:'vol',time:'time',video:'vid'}[pt]); if(!e) return; const base=pt==='cover'?oArtFilter(p):''; const all=[(base&&base!=='none')?base:'', fxFilter(p,pt)].filter(Boolean).join(' '); e.style.filter=all||'none'; e.style.boxShadow=fxBoxShadow(p,pt)||''; });
    wrap.classList.toggle('nobg', p.bg_enabled===false);
    show('art', p.show_art); show('title', p.show_title); show('artistname', p.show_artist); show('bars', p.show_bars); show('prog', p.show_prog); show('vol', p.show_vol===true); show('time', p.show_time===true);
    coverOn = p.cover_bg===true;
    if(!coverOn) document.getElementById('coverbg').classList.remove('on');
    const L=p.layout||{}, bg=L.bg||{x:0,y:0,w:252,h:84}, cov=L.cover||{x:12,y:12,size:60}, tx=L.text||{x:84,y:20}, pr=L.prog||{x:84,y:74,w:150,h:4}, ba=L.bars||{x:150,y:40,w:40,h:14}, vo=L.vol||{x:196,y:92,w:56,h:26}, ti=L.time||{x:84,y:92,w:96,h:16};
    pos('bg', bg); pos('art', {x:cov.x,y:cov.y,w:cov.size,h:cov.size}); pos('meta', {x:tx.x,y:tx.y}); pos('prog', {x:pr.x,y:pr.y,w:pr.w,h:pr.h||4}); pos('vol', vo); pos('time', ti); { const te=document.getElementById('time'); if(te){ te.style.fontSize=((ti.h||16)*0.62)+'px'; te.style.width='auto'; te.style.height='auto'; te.style.transform=(p.time_align==='center'?'translateX(-50%)':p.time_align==='right'?'translateX(-100%)':'none'); } }
    pos('vid', L.video||{x:0,y:0,w:252,h:84}); applyVideo(p, document.getElementById('vid'));
    { const vz=document.getElementById('viz'); if(vz){ pos('viz', L.viz||{x:0,y:0,w:252,h:84}); vz.style.display=(p.show_viz===true)?'block':'none'; vz.style.borderRadius=(p.viz_radius||0)+'px'; } syncViz(); }
    const battach=p.bars_attach!==false; placeBars(battach); if(!battach) pos('bars', ba);
    ((L.order)||['bg','viz','video','cover','text','bars','prog','vol','time']).forEach((pt,i)=>{ const e=document.getElementById(EID[pt]); if(e){ e.style.zIndex=i; e.style.opacity=(p.op&&p.op[pt]!=null)?p.op[pt]:1; } });
    ['bg','viz','video','cover','text','bars','prog','vol','time'].forEach(oClip);
    fitWrap();   // scale the cropped content to fill the OBS source (responsive)
    document.getElementById('meta').style.transformOrigin='top left';
    document.getElementById('meta').style.transform='scale('+(tx.scale||1)+')';
    document.getElementById('meta').style.alignItems=(p.text_align==='center'?'center':p.text_align==='right'?'flex-end':'flex-start');
    const tsc=tx.scale||1, hasTW=tx.w!=null; document.getElementById('meta').style.width=hasTW?(tx.w/tsc)+'px':'';   // fixed text-box width (resizable) or auto-fit
    const clip=hasTW?(tx.w/tsc):Math.max(40,((bg.x+bg.w)-tx.x-14)/tsc);
    document.getElementById('title').style.maxWidth=clip+'px';
    document.getElementById('artistname').style.maxWidth=Math.max(30,clip-30)+'px';
    if(clip!==_clipW){ _clipW=clip; marquee('title'); marquee('artistname'); }
  }catch(e){}
}
let lastVer=null, coverOn=false, barsPaused=false, _autoRGB=null, _lastP={}, _clipW=null, _barLv=null, _vol=1, _volTouch=0;
let npPos=0, npDur=0, npPlaying=false, npAt=0, _lastClkVer=null, _npVideo='';
function fmt(s){ s=Math.max(0,Math.floor(s||0)); return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2); }
function updProg(){ const t=npPos+(npPlaying?(performance.now()-npAt)/1000:0);
  const f=document.getElementById('progfill'); if(f) f.style.width=((npDur>0?Math.max(0,Math.min(1,t/npDur)):0)*100)+'%';
  const te=document.getElementById('time'); if(te) te.textContent=fmt(t)+' / '+fmt(npDur); }
setInterval(updProg, 250);
async function fetchBars(){ try{ const d=await(await fetch('/bars',{cache:'no-store'})).json();
  _barLv=(Array.isArray(d.bars)&&d.bars.length>=3)?d.bars:null; }catch(e){} }
setInterval(fetchBars, 40);   // fast poll so the bars track the music tightly
// VISUALIZER layer: WebGL Silk/Aurora field driven by the live /bars audio, palette
// auto-from-cover (viz_layer.js). Engine 404s in viz-free builds -> guards no-op.
let vizL=null, _vizMode=null, _vizArtSrc=null;
function hx3(h){ const m=/^#?([0-9a-f]{6})$/i.exec(h||''); if(!m) return [1,1,1];
  const n=parseInt(m[1],16); return [(n>>16&255)/255,(n>>8&255)/255,(n&255)/255]; }
function syncViz(){ if(!vizL||!_lastP) return; const v=_lastP.viz||{};
  if(v.mode && v.mode!==_vizMode){ vizL.setMode(v.mode); _vizMode=v.mode; }
  vizL.setConfig({speed:v.speed, scale:v.scale, sharp:v.sharp, vintage:v.vintage, rattle:v.rattle,
    smooth:v.smooth, flash:v.flash, beatPal:v.beatPal||'off',
    palCount:(v.auto?5:(Array.isArray(v.pal)?v.pal.length:5))});
  if(v.auto){ const A=document.getElementById('art');   // /art is same-origin -> readable, no taint
    if(A&&A.complete&&A.naturalWidth&&A.src!==_vizArtSrc){ _vizArtSrc=A.src; vizL.setPaletteFromImage(A); } }
  else if(Array.isArray(v.pal)){ _vizArtSrc=null; vizL.setPalette(v.pal.map(hx3)); }
  vizL.setMono(v.mono); vizL.setSpectrum(v.spectrum); vizL.setLocks(v.lock); }
(function(){ const cv=document.getElementById('viz'); if(!cv||!window.VizLayer) return;
  try{ vizL=new VizLayer(cv); }catch(e){ console.error('viz init',e); return; }
  function draw(){ if(vizL && _lastP && _lastP.show_viz===true){ vizL.pushBands(_barLv); vizL.render(); }
    requestAnimationFrame(draw); } requestAnimationFrame(draw); })();
// Volume layer: speaker glyph + sound-wave arcs that light up with loudness (mean of the EQ bands).
(function(){ const cv=document.getElementById('vol'); if(!cv||!cv.getContext) return;
  const ctx=cv.getContext('2d'); let lvl=0;
  function rr(x,y,ww,hh,r){ ctx.beginPath(); if(ctx.roundRect) ctx.roundRect(x,y,ww,hh,r); else ctx.rect(x,y,ww,hh); ctx.fill(); }
  function draw(){ const d=window.devicePixelRatio||1, rect=cv.getBoundingClientRect();
    const wW=Math.max(2,Math.round(rect.width*d)), wH=Math.max(2,Math.round(rect.height*d));
    if(cv.width!==wW) cv.width=wW; if(cv.height!==wH) cv.height=wH;
    const w=cv.width, h=cv.height; ctx.clearRect(0,0,w,h);
    const col=(getComputedStyle(cv).getPropertyValue('--vol')||getComputedStyle(cv).getPropertyValue('--accent')||'#fff').trim();
    const tgt=Math.max(0,Math.min(1,(_vol==null?0.6:_vol))); lvl+=(tgt-lvl)*0.25;   // SET volume level (not loudness)
    ctx.fillStyle=col; ctx.strokeStyle=col; ctx.lineCap='round'; ctx.lineJoin='round';
    const idle=performance.now()-_volTouch, _fade=idle<1600?1:Math.max(0,1-(idle-1600)/450);   // OSD auto-fade: full ~1.6s after a change, then fade out
    cv.style.opacity=_fade*((_lastP.op&&_lastP.op.vol!=null)?_lastP.op.vol:1);
    const bar=!!(_lastP&&_lastP.vol_style==='bar');
    if(bar){   // box-relative: speaker + waves sized to box height (left); level bar fills the rest of the width -> widen the layer to lengthen the bar
      const sy=h/2, sx=h*0.12, bw=h*0.13, bh=h*0.28;
      ctx.lineWidth=Math.max(1.5,h*0.07);
      ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.5); ctx.lineTo(sx+bw,sy-bh*0.5); ctx.lineTo(sx+bw*2.1,sy-bh);
        ctx.lineTo(sx+bw*2.1,sy+bh); ctx.lineTo(sx+bw,sy+bh*0.5); ctx.lineTo(sx,sy+bh*0.5); ctx.closePath();
      ctx.fill(); ctx.stroke();
      if(lvl<=0.01){ ctx.save(); ctx.lineCap='round';   // muted -> slash over the speaker, no waves/bar
        ctx.globalCompositeOperation='destination-out'; ctx.lineWidth=h*0.12; ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
        ctx.globalCompositeOperation='source-over'; ctx.lineWidth=Math.max(1.5,h*0.055); ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
        ctx.restore(); requestAnimationFrame(draw); return; }
      const ax=sx+bw*2.6; ctx.lineWidth=Math.max(1.5,h*0.05);
      for(let i=0;i<2;i++){ const r=h*(0.15+i*0.11); ctx.globalAlpha=lvl>(i+0.2)/2?0.9:0.18;
        ctx.beginPath(); ctx.arc(ax,sy,r,-Math.PI/4.5,Math.PI/4.5); ctx.stroke(); }
      ctx.globalAlpha=1;
      const tx0=ax+h*0.60, tx1=w-h*0.10, tw=Math.max(h*0.18,tx1-tx0), th=h*0.16, ry=th/2;
      ctx.globalAlpha=0.22; rr(tx0,sy-ry,tw,th,ry);
      ctx.globalAlpha=1; rr(tx0,sy-ry,Math.max(th,tw*lvl),th,ry);
      requestAnimationFrame(draw); return;
    }
    const CL=0.02, CW=1.06, CH=0.80, PAD=0.94;   // arcs: uniform-fit content bbox (incl. arc reach ax+r + stroke)
    const u=Math.min(w*PAD/CW, h*PAD/CH), sy=0.5*u; ctx.save(); ctx.translate(w/2-(CL+CW/2)*u, h/2-0.5*u);
    const bw=u*0.16, bh=u*0.30, sx=u*0.10;
    ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.5); ctx.lineTo(sx+bw,sy-bh*0.5); ctx.lineTo(sx+bw*2.1,sy-bh);
      ctx.lineTo(sx+bw*2.1,sy+bh); ctx.lineTo(sx+bw,sy+bh*0.5); ctx.lineTo(sx,sy+bh*0.5); ctx.closePath();
    ctx.lineWidth=Math.max(1.5,u*0.14); ctx.fill(); ctx.stroke();
    if(lvl<=0.01){ ctx.save(); ctx.lineCap='round';   // muted -> slash over the speaker, no waves
      ctx.globalCompositeOperation='destination-out'; ctx.lineWidth=u*0.12; ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
      ctx.globalCompositeOperation='source-over'; ctx.lineWidth=Math.max(1.5,u*0.055); ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
      ctx.restore(); ctx.restore(); requestAnimationFrame(draw); return; }
    const ax=sx+bw*2.5; ctx.lineWidth=Math.max(1.5,u*0.06);
    for(let i=0;i<3;i++){ const on=lvl>(i+0.15)/3, r=u*(0.20+i*0.17);
      ctx.globalAlpha=on?Math.min(1,0.45+lvl*0.55):0.13;
      ctx.beginPath(); ctx.arc(ax,sy,r,-Math.PI/4.5,Math.PI/4.5); ctx.stroke(); }
    ctx.globalAlpha=1; ctx.restore(); requestAnimationFrame(draw); }
  requestAnimationFrame(draw); })();
// EQ bars on a canvas: drawn at integer device pixels -> crisp + uniform at any DPR,
// cleared each frame (no ghosting). Same draw takes real audio levels later.
(function(){
  const cv=document.getElementById('bars'); if(!cv||!cv.getContext) return;
  const ctx=cv.getContext('2d');
  const ph=[0,1.7,3.1,0.8,2.3], sp=[5.5,7.2,4.6,7.8,6.2], lo=[.42,.52,.38,.58,.48], hi=[1,.82,.95,1,.74];
  let disp=[], t0=null;
  function grp(arr,i,N){ const M=arr.length, a=Math.floor(i*M/N), b=Math.max(a+1,Math.floor((i+1)*M/N)); let s=0; for(let j=a;j<b;j++) s+=arr[j]||0; return s/(b-a); }
  function draw(ts){ if(t0===null)t0=ts; const t=(ts-t0)/1000, d=window.devicePixelRatio||1;
    const rect=cv.getBoundingClientRect();
    const wW=Math.max(2,Math.round(rect.width*d)), wH=Math.max(2,Math.round(rect.height*d));
    if(cv.width!==wW) cv.width=wW; if(cv.height!==wH) cv.height=wH;
    const h=cv.height, N=Math.max(3,Math.min(12,(_lastP.bars_n||5)));
    if(disp.length!==N) disp=new Array(N).fill(0.5);
    const gap=(_lastP.bars_gap==null?0.4:_lastP.bars_gap), round=_lastP.bars_round!==false, glow=_lastP.bars_glow===true;
    const pitch=cv.width/N, GAPd=Math.max(0,Math.round(pitch*gap)), BWd=Math.max(1,Math.round(pitch-GAPd));
    ctx.clearRect(0,0,cv.width,h);
    const col=(getComputedStyle(cv).getPropertyValue('--accent')||'#fff').trim();
    ctx.fillStyle=col; ctx.shadowBlur=glow?Math.max(4,BWd*0.9):0; ctx.shadowColor=glow?col:'transparent';
    const useLv = _barLv && (_lastP.bars_reactive!==false);
    for(let i=0;i<N;i++){
      const tgt = useLv ? Math.max(0,Math.min(1,grp(_barLv,i,N)))                               // real music (12 bands grouped to N)
                        : (barsPaused?lo[i%5]:lo[i%5]+(hi[i%5]-lo[i%5])*(0.5+0.5*Math.sin(t*sp[i%5]+ph[i%5])));   // canned animation
      disp[i] += (tgt-disp[i]) * (useLv?0.55:0.18);
      const bh=Math.max(BWd,disp[i]*h), x=Math.round(i*pitch+GAPd/2), y=h-bh, r=round?Math.min(BWd/2,bh/2):0;
      ctx.beginPath();
      if(round&&ctx.roundRect){ ctx.roundRect(x,y,BWd,bh,[r,r,r,r]); } else { ctx.rect(x,y,BWd,bh); }
      ctx.fill();
    }
    ctx.shadowBlur=0;
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();
async function tick(){
  try{
    const d=await (await fetch('/np',{cache:'no-store'})).json();
    wrap.classList.toggle('show', !!(d.title||d.artist));
    barsPaused = !d.playing;
    _barLv = (Array.isArray(d.bars) && d.bars.length>=3) ? d.bars : null;   // real EQ levels (null -> canned animation)
    { const nv=(typeof d.volume==='number')?d.volume:_vol; if(Math.abs(nv-_vol)>0.002) _volTouch=performance.now(); _vol=nv; }   // track changes -> auto-fade the volume OSD when idle
    { const play=!!d.playing; if(npPlaying!==play||d.ver!==_lastClkVer){ npPos=d.pos||0; npAt=performance.now(); _lastClkVer=d.ver; } npDur=d.dur||0; npPlaying=play; } _npVideo=d.video||'';   // free local clock (resync on play/track) + live Canvas/video URL
    if(d.ver!==lastVer){ lastVer=d.ver;
      document.getElementById('title').textContent=d.title||'Nothing playing';
      document.getElementById('artistname').textContent=d.artist||'';
      marquee('title'); marquee('artistname');
      const A=document.getElementById('art'), cb=document.getElementById('coverbg');
      if(d.art){ const u='/art?v='+encodeURIComponent(d.ver); A.classList.remove('noart');
        A.onload=()=>{ if(_lastP.bg_auto||_lastP.accent_auto||_lastP.prog_auto||anyFxAuto(_lastP)){ _autoRGB=sampleArt(A);
          R.style.setProperty('--bg', bgCss(_lastP)); R.style.setProperty('--accent', accentCss(_lastP)); R.style.setProperty('--prog', progCss(_lastP)); R.style.setProperty('--vol', volCss(_lastP)); } };
        A.src=u; if(coverOn){ cb.style.backgroundImage="url('"+u+"')"; cb.classList.add('on'); } }
      else { A.onload=null; A.classList.add('noart'); A.src=NOART; cb.classList.remove('on'); } }
  }catch(e){}
}
applyPreset(); tick(); setInterval(tick,800); setInterval(applyPreset,600);
let _boot=null; setInterval(async()=>{ try{ const b=(await(await fetch('/boot',{cache:'no-store'})).json()).boot;
  if(_boot&&b!==_boot) location.reload(); _boot=b; }catch(e){} }, 1500);   // auto-reload when Segue restarts (new JS)
</script></body></html>"""
_EDITOR_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Segue overlay editor</title>
<style>
  *{ box-sizing:border-box; }
  @font-face{ font-family:'Inter'; src:url('/font/inter.ttf') format('truetype'); font-weight:100 900; font-style:normal; font-display:swap; }
  body{ margin:0; font-family:'Inter','Segoe UI',system-ui,sans-serif; font-weight:500; background:#1e1e1e; color:#f0f0f0;
    display:flex; align-items:flex-start; justify-content:center; min-height:100vh; overflow:auto; }
  .editor{ display:flex; flex-direction:column; align-items:center; gap:16px; padding:26px;
    width:100%; max-width:1180px; }
  .topbar{ display:flex; flex-direction:column; align-items:center; gap:6px; position:relative; }
  .subrow{ display:flex; align-items:center; gap:10px; }
  .subtitle{ font-size:14.5px; color:#b6b6b2; letter-spacing:5px; text-transform:uppercase; font-weight:600; }
  #work{ display:flex; gap:16px; align-items:flex-start; width:100%; }
  #stagecol{ flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:10px; }
  #obsbar{ background:#232321; border-radius:10px; padding:12px 14px; display:flex; flex-wrap:wrap; gap:12px 24px; align-items:flex-end; }
  #presetbar{ background:#232321; border-radius:10px; padding:12px 14px; display:flex; flex-wrap:wrap; gap:12px 18px; align-items:flex-end; }
  .presetbtns{ display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; }
  .obsfield{ display:flex; flex-direction:column; gap:7px; }
  .obsfield>label{ font-size:10.5px; color:#9a9a98; font-weight:600; letter-spacing:.6px; text-transform:uppercase; }
  #dock{ flex:0 0 296px; min-width:0; max-width:296px; display:flex; flex-direction:column; gap:10px; }
  .dpanel{ background:#232321; border-radius:9px; overflow:hidden; display:flex; flex-direction:column; max-height:calc(100vh - 130px); }
  .dhead{ font-size:12.5px; font-weight:700; letter-spacing:.3px; color:#ededeb;
    padding:11px 14px; border-bottom:1px solid #1b1b19; display:flex; align-items:center; gap:9px;
    cursor:pointer; user-select:none; background:#262624; transition:background-color .12s; }
  .dhead::before{ content:""; flex:0 0 auto; width:3px; height:13px; border-radius:2px; background:#FF7A1A; }
  .dhead:hover{ background:#2c2c29; }
  .dhead.static{ cursor:default; } .dhead.static:hover{ background:#262624; }
  .dhead .chev{ margin-left:auto; display:flex; opacity:.5; transition:transform .18s ease, opacity .12s; }
  .dhead .chev svg{ width:13px; height:13px; }
  .dhead:hover .chev{ opacity:.85; }
  .dpanel.collapsed .dhead{ border-bottom-color:transparent; }
  .dpanel.collapsed .dhead .chev{ transform:rotate(-90deg); }
  .dpanel.collapsed .dbody{ display:none; }
  .dbody{ padding:13px; display:flex; flex-direction:column; gap:14px; overflow-y:auto; min-height:0; }   // scroll internally when properties get long (no runaway sidebar)
  .propsep{ padding-top:13px; border-top:1px solid #313130; font-size:11.5px; font-weight:700; letter-spacing:.3px; color:#cfcfcc; }
  .sclist{ display:flex; flex-direction:column; gap:9px; }
  #fxbody{ position:fixed; display:none; flex-direction:column; gap:11px; z-index:60; width:286px; max-height:82vh; overflow-y:auto; background:#232321; border:1px solid #36352f; border-radius:11px; padding:12px; box-shadow:0 16px 46px rgba(0,0,0,.6); }
  #fxbody.open{ display:flex; }
  #fxpophead{ display:flex; align-items:center; justify-content:space-between; }
  #fxpoptitle{ font-size:12px; font-weight:700; letter-spacing:.3px; color:#ededeb; }
  #fxpopx{ cursor:pointer; color:#9a9a98; font-size:20px; line-height:1; padding:0 4px; }
  #fxpopx:hover{ color:#fff; }
  .lfx{ font-size:10.5px; font-weight:600; color:#9a9a98; cursor:pointer; padding:2px 7px; border-radius:5px; flex:0 0 auto; }
  .lfx:hover{ color:#fff; background:#34332f; }
  .lfx.on{ color:#FF7A1A; }
  .fxcard{ background:#1e1e1c; border:1px solid #2d2d2a; border-radius:9px; padding:11px 12px; }
  .fxtog{ display:flex; align-items:center; gap:9px; margin:0; font-size:12.5px; font-weight:600; color:#ededeb; cursor:pointer; }
  .fxgroup{ display:flex; flex-direction:column; gap:10px; margin-top:11px; padding-top:11px; border-top:1px solid #2d2d2a; }
  .fxgroup .fld{ flex-direction:row; align-items:center; gap:10px; }
  .fxgroup .fld>label{ flex:0 0 64px; text-align:left; text-transform:none; letter-spacing:0; font-weight:500; font-size:11.5px; color:#bdbdb8; }
  .fxgroup .fld input[type=range]{ flex:1 1 auto; min-width:0; }
  .fxgroup .fld input[type=color]{ flex:1 1 auto; min-width:0; height:26px; }
  #help{ position:absolute; top:8px; left:8px; z-index:8; width:28px; height:28px; border-radius:50%; background:#3a3a36; color:#e9e9e4; font-size:17px; font-weight:700; display:flex; align-items:center; justify-content:center; cursor:help; user-select:none; border:1px solid #56564e; box-shadow:0 2px 8px rgba(0,0,0,.4); }
  #help:hover{ background:#46463f; color:#fff; }
  #helppop{ position:absolute; top:34px; left:0; width:236px; background:#232321; border-radius:9px; padding:12px 13px; display:none; box-shadow:0 10px 28px rgba(0,0,0,.55); z-index:9; cursor:default; }
  #help:hover #helppop{ display:block; }
  .transport{ display:flex; justify-content:flex-start; gap:7px; }
  .tbtn{ height:38px; min-width:54px; border:none; border-radius:9px; background:#2b2b29; color:#ededeb; display:inline-flex; align-items:center; justify-content:center; cursor:pointer; transition:background .12s; }
  .tbtn:hover{ background:#34332f; }
  .tbtn:active{ transform:translateY(1px); }
  .tbtn.mid{ min-width:70px; background:#ededeb; color:#1b1b19; }
  .tbtn.mid:hover{ background:#fff; }
  .tbtn svg{ width:19px; height:19px; }
  .footer{ display:flex; align-items:center; justify-content:center; gap:9px; font-size:13px; color:#cfcfcc; margin-top:3px; }
  .footer .flink{ display:inline-flex; align-items:center; gap:6px; color:#ededeb; text-decoration:none; font-weight:600; }
  .footer .flink:hover{ color:#fff; }
  .footer .flink img{ height:16px; width:auto; }
  .footer .fdim{ color:#8a8a88; font-weight:400; }
  .footer .fdot{ color:#55554f; }
  .angwheel{ flex:0 0 auto; margin-left:auto; width:30px; height:30px; border-radius:50%; background:radial-gradient(circle at 50% 34%, #34342f, #201f1e); border:1px solid #161514; box-shadow:inset 0 1px 2px rgba(0,0,0,.55), inset 0 -1px 1px rgba(255,255,255,.05); position:relative; cursor:pointer; }
  .angwheel:hover{ border-color:#3a3a34; }
  .anghand{ position:absolute; left:50%; top:50%; width:10px; height:0; transform-origin:left center; transform:rotate(90deg); }
  .anghand::after{ content:''; position:absolute; right:-3px; top:-3px; width:6px; height:6px; border-radius:50%; background:#FF7A1A; box-shadow:0 0 5px rgba(255,122,26,.6); }
  .scrow{ display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:12px; color:#bdbdb9; }
  .scrow span:first-child{ color:#9a9a98; }
  kbd{ font-family:inherit; font-size:10.5px; font-weight:600; color:#d8d8d4; background:#2c2c29; border-radius:4px; padding:2px 6px; white-space:nowrap; }
  .hint{ font-size:11.5px; color:#7a7a78; text-align:center; max-width:560px; }
  .canvrow{ display:flex; align-items:center; gap:8px; justify-content:space-between; }
  #canvdim{ font-size:12.5px; color:#c2c2c0; font-variant-numeric:tabular-nums; }
  .fitb{ background:#2b2b29; color:#e6e6e3; border:1px solid #1b1b19; border-radius:7px; padding:6px 10px;
    font-size:12px; cursor:pointer; box-shadow:inset 0 1px 2px rgba(0,0,0,.25); }
  .fitb:hover{ background:#323230; }
  #opwrap{ display:flex; align-items:center; gap:8px; margin-bottom:9px; font-size:11px; color:#9a9a98; }
  #opwrap.dim{ opacity:.4; pointer-events:none; }
  .prophint{ font-size:11.5px; color:#7a7a78; line-height:1.4; }
  #opwrap .oplbl{ text-transform:uppercase; letter-spacing:.5px; }
  #opwrap input[type=range]{ flex:1; width:auto; min-width:0; }
  #opval{ min-width:36px; text-align:right; color:#c2c2c0; font-variant-numeric:tabular-nums; }
  .lrow.sel{ background:#39322a; }
  .urlrow{ display:flex; gap:8px; align-items:center; }
  #obsurl{ flex:1; min-width:0; height:32px; background:#2b2b29; color:#c2c2c0; border:1px solid #1b1b19;
    border-radius:7px; padding:0 9px; font-size:12px; box-shadow:inset 0 1px 2px rgba(0,0,0,.25); }
  .logo{ height:46px; width:auto; opacity:.95; }
  /* the preview box: a medium framed window in the middle, overlay centered inside */
  #stage{ flex:0 0 auto; min-width:0; height:560px; position:relative; display:flex;
    align-items:center; justify-content:center; border-radius:12px; border:1px solid #1b1b19;
    overflow:hidden; user-select:none; -webkit-user-select:none;
    background:repeating-conic-gradient(#262626 0% 25%,#202020 0% 50%) 0/26px 26px; }
  /* pan scrollbars (Segue style): appear when the canvas overflows the stage */
  #hscroll{ position:absolute; left:4px; right:13px; bottom:3px; height:9px; display:block; z-index:7; }
  #vscroll{ position:absolute; top:4px; bottom:13px; right:3px; width:9px; display:block; z-index:7; }
  #hscroll .sthumb{ position:absolute; top:1px; height:7px; min-width:28px; border-radius:4px; }
  #vscroll .sthumb{ position:absolute; left:1px; width:7px; min-height:28px; border-radius:4px; }
  .sthumb{ background:#484845; cursor:pointer; transition:background-color .12s; }
  .sthumb:hover{ background:#5e5e5a; }
  /* overlay canvas (the OBS source bounds), shown at a fixed zoom; elements absolute inside */
  #cv{ position:relative; transform:scale(2); transform-origin:center center;
    --bg:rgba(43,43,41,.85); --text:#fff; --sub:#c8c8d0; --accent:#fff; --art-radius:10px; --bg-radius:13px; }
  #bg{ position:absolute; background:var(--bg); border-radius:var(--bg-radius); overflow:hidden; transition:background-color .5s ease; }
  #cv.nobg #bg{ display:none; }
  #coverbg{ position:absolute; inset:0; background-size:cover; background-position:center;
    filter:blur(16px) brightness(.55) saturate(1.3); transform:scale(1.3); opacity:0; }
  #coverbg.on{ opacity:1; }
  #art{ position:absolute; object-fit:cover; background:#3a3a38; border-radius:var(--art-radius); }
  #art.hidden{ display:none; } #art.noart{ object-fit:contain; }
  #meta{ position:absolute; display:flex; flex-direction:column; align-items:flex-start; font-family:'Segoe UI',Inter,system-ui,sans-serif; } #artistname.hidden{ display:none; }
  #title{ color:var(--text); font-weight:700; font-size:17px; line-height:1.15; overflow:hidden; white-space:nowrap; }
  #title.hidden,#bars.hidden{ display:none; }
  #artist{ color:var(--sub); font-weight:500; font-size:13.5px; margin-top:3px; display:flex;
    align-items:center; gap:8px; }
  #artistname{ display:inline-block; overflow:hidden; white-space:nowrap; }
  .faded{ -webkit-mask-image:linear-gradient(to right,transparent 0,#000 16px,#000 calc(100% - 16px),transparent 100%);
          mask-image:linear-gradient(to right,transparent 0,#000 16px,#000 calc(100% - 16px),transparent 100%); }
  #bars{ position:absolute; } #vol{ position:absolute; } #vol.hidden{ display:none; }
  #prog{ position:absolute; height:4px; border-radius:3px; background:rgba(255,255,255,.22); overflow:hidden; }
  #time{ position:absolute; color:var(--sub); font-weight:600; font-variant-numeric:tabular-nums; white-space:nowrap; line-height:0.9; display:flex; align-items:center; }
  #vid{ position:absolute; object-fit:cover; border-radius:var(--bg-radius); display:none; }
  #time.hidden{ display:none; }
  #progfill{ height:100%; width:0%; background:var(--prog,var(--accent)); border-radius:3px; }
  #bg,#art,#meta,#prog,#vol{ cursor:move; }
  /* resize handles: in #stage (screen space), JS-pinned to a corner so they stay a
     constant size + read as real handles (don't scale with the canvas zoom). */
  .selbox{ position:absolute; border:1.5px solid #FF7A1A; box-sizing:border-box; border-radius:4px;
    pointer-events:none; z-index:8; display:none; }
  .hand{ position:absolute; width:12px; height:12px; border-radius:3px; background:#fff;
    border:1.5px solid #FF7A1A; cursor:nwse-resize; z-index:10; box-shadow:0 1px 4px rgba(0,0,0,.45); display:none; }
  .hand.on{ display:block; }
  .radh{ position:absolute; width:12px; height:12px; border-radius:50%; background:#fff; border:1.5px solid #FF7A1A;
    cursor:grab; z-index:11; box-shadow:0 1px 5px rgba(0,0,0,.5); display:none; }
  .radh:active{ cursor:grabbing; }
  /* alignment guides (shown while snapping with Shift) */
  .guide{ position:absolute; background:#FF7A1A; display:none; z-index:5; pointer-events:none; }
  #gx{ width:1px; top:0; } #gy{ height:1px; left:0; }
  #marq{ position:absolute; border:1px solid #FF7A1A; background:rgba(255,122,26,.12); z-index:9; display:none; pointer-events:none; }
  /* dock panels (Layers / Properties / Colors) replace the old bottom #panel */
  .fld{ display:flex; flex-direction:column; gap:8px; } .fld>label{ font-size:10.5px; color:#9a9a98; text-align:center;
    font-weight:600; letter-spacing:.7px; text-transform:uppercase; }
  select{ -webkit-appearance:none; appearance:none; height:38px; width:100%; background:#2b2b29; color:#f0f0f0;
    border:1px solid #1b1b19; border-radius:8px; padding:0 34px 0 13px; font-size:14px; cursor:pointer;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%239a9a98' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>");
    background-repeat:no-repeat; background-position:right 12px center;
    box-shadow:inset 0 1px 2px rgba(0,0,0,.25); transition:background-color .15s ease, border-color .15s ease; }
  select:hover{ background-color:#323230; }
  select:focus{ outline:none; border-color:#FF7A1A; }
  .seg{ display:flex; gap:4px; }
  .seg button{ flex:1; height:34px; background:#2b2b29; color:#9a9a98; border:1px solid #1b1b19; border-radius:7px;
    cursor:pointer; display:flex; align-items:center; justify-content:center; box-shadow:inset 0 1px 2px rgba(0,0,0,.25); transition:background-color .12s, color .12s; }
  .seg button:hover{ color:#cfcfcc; }
  .seg button.on{ color:#1f1f1e; background:#FF7A1A; border-color:#FF7A1A; }
  input[type=range]{ -webkit-appearance:none; appearance:none; width:200px; height:4px; border-radius:3px;
    background:#3a3a38; outline:none; cursor:pointer; }
  input[type=range]::-webkit-slider-thumb{ -webkit-appearance:none; width:16px; height:16px; border-radius:50%;
    background:#f0f0f0; border:none; box-shadow:0 1px 3px rgba(0,0,0,.45); cursor:pointer; }
  input[type=color]{ -webkit-appearance:none; appearance:none; width:100%; height:36px; border:1px solid #1b1b19;
    border-radius:8px; background:none; cursor:pointer; padding:0; box-shadow:inset 0 1px 2px rgba(0,0,0,.3); transition:transform .1s ease; }
  input[type=color]:hover{ transform:translateY(-1px); }
  input[type=color]::-webkit-color-swatch-wrapper{ padding:3px; }
  input[type=color]::-webkit-color-swatch{ border:none; border-radius:5px; }
  .colors{ display:flex; gap:10px; justify-content:space-between; }
  .col{ display:flex; flex-direction:column; align-items:center; gap:7px; font-size:10.5px; color:#9a9a98; width:54px; text-transform:uppercase; letter-spacing:.4px; }
  .ac{ font-size:11px; color:#9a9a9a; display:flex; gap:6px; align-items:center; justify-content:center; margin-top:8px; }
  .chks{ display:flex; gap:16px; } .chk{ display:inline-flex; gap:7px; align-items:center; font-size:14px; }
  .chk input{ width:17px; height:17px; }
  #layers{ display:flex; flex-direction:column; gap:3px; min-width:200px; }
  .lrow{ display:flex; align-items:center; gap:8px; background:#262624; border-radius:6px; position:relative;
    padding:5px 8px; font-size:13px; cursor:grab; transition:background-color .12s ease; }
  .lrow:hover{ background:#2e2e2b; }
  .lrow:active{ cursor:grabbing; }
  .lrow.drag{ opacity:.45; }
  .lrow.clipped{ margin-left:16px; }
  .lrow.over-top::after,.lrow.over-bottom::after{ content:''; position:absolute; left:0; right:0; height:2px; background:#FF7A1A; pointer-events:none; }
  .lrow.over-top::after{ top:-3px; } .lrow.over-bottom::after{ bottom:-3px; }
  .leye{ flex:0 0 18px; width:18px; height:18px; color:#cfcfcc; cursor:pointer; display:flex; align-items:center; justify-content:center; }
  .leye.off{ color:#55554f; }
  .lthumb{ flex:0 0 30px; width:30px; height:30px; border-radius:5px; overflow:hidden; background:#16160f;
    display:flex; align-items:center; justify-content:center; }
  .lthumb img{ width:100%; height:100%; object-fit:cover; display:block; }
  .lname{ flex:1; color:#e6e6e3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .lname.base{ text-decoration:underline; text-decoration-color:#FF7A1A; text-underline-offset:3px; text-decoration-thickness:2px; }
  .lclip{ flex:0 0 auto; padding:2px 8px; height:20px; color:#8a8a86; cursor:pointer; border-radius:5px; opacity:0; font-size:11px; font-weight:600;
    display:flex; align-items:center; transition:opacity .12s ease, color .12s ease, background-color .12s ease; }
  .lrow:hover .lclip{ opacity:1; }
  .lclip:hover{ color:#fff; background:#33332f; } .lclip.on{ opacity:1; color:#FF7A1A; }
  .llock{ flex:0 0 auto; width:20px; height:22px; color:#46463f; cursor:pointer; border-radius:5px; opacity:1;
    display:flex; align-items:center; justify-content:center; transition:color .12s, background-color .12s; }
  .llock:hover{ color:#a0a09c; background:#33332f; } .llock.on{ color:#FF7A1A; }
  .carrow{ position:absolute; left:-16px; top:50%; transform:translateY(-38%); display:flex; pointer-events:none; }
  .tbars{ display:flex; align-items:flex-end; gap:2px; height:14px; }
  .tbars i{ width:3px; background:currentColor; border-radius:1px; }
  .tbars i:nth-child(1){ height:7px; } .tbars i:nth-child(2){ height:13px; } .tbars i:nth-child(3){ height:9px; }
  #saved{ position:fixed; top:14px; right:18px; font-size:11px; font-weight:600; color:#3FB950; background:#1e2a1e; border-radius:6px; padding:4px 11px; opacity:0; transition:opacity .2s; pointer-events:none; z-index:50; }
  input[type=checkbox]{ accent-color:#f0f0f0; }
</style></head><body>
<div class="editor">
<div class="topbar"><img class="logo" src="/logo" alt="Segue">
  <span class="subtitle">Overlay Editor</span>
  <div class="footer"><a class="flink" href="https://ko-fi.com/segueapp" target="_blank" rel="noopener"><img src="/kofi.png" alt=""> Support me</a> <span class="fdim">if you wanna</span> <span class="fdot">·</span> <a class="flink" href="https://discord.gg/AUrMXdzGZE" target="_blank" rel="noopener">Join the Discord <img src="/discord.png" alt=""></a></div>
  <span id="saved">saved</span></div>
<div id="work">
<div id="stagecol">
<div id="stage">
  <div id="help">?<div id="helppop"><div class="sclist">
    <div class="scrow"><span>Move</span><kbd>drag</kbd></div>
    <div class="scrow"><span>Resize from centre</span><kbd>Alt</kbd></div>
    <div class="scrow"><span>Keep aspect</span><span><kbd>Alt</kbd>+<kbd>Shift</kbd></span></div>
    <div class="scrow"><span>Lock axis / no snap</span><kbd>Shift</kbd></div>
    <div class="scrow"><span>Multi-select</span><span><kbd>Shift</kbd>+click · marquee</span></div>
    <div class="scrow"><span>Zoom canvas</span><span><kbd>Alt</kbd>+scroll</span></div>
    <div class="scrow"><span>Pan canvas</span><span><kbd>scroll</kbd> · middle-drag</span></div>
    <div class="scrow"><span>Reset view</span><kbd>0</kbd></div>
    <div class="scrow"><span>Undo · Redo</span><span><kbd>Ctrl</kbd>+<kbd>Z</kbd> · <kbd>Y</kbd></span></div>
  </div></div></div>
  <div id="cv">
    <div id="bg"><div id="coverbg"></div></div>
    <img id="art" alt="">
    <div id="meta"><div id="title">Title</div>
      <div id="artist"><span id="artistname">Artist</span></div></div>
    <canvas id="bars" width="23" height="11"></canvas>
    <div id="prog"><div id="progfill"></div></div>
    <canvas id="vol" width="56" height="26"></canvas>
    <div id="time">0:00 / 0:00</div>
    <video id="vid" muted loop playsinline></video>
    <div class="guide" id="gx"></div><div class="guide" id="gy"></div>
  </div>
  <div id="marq"></div>
  <div id="hscroll"><div class="sthumb" id="hthumb"></div></div>
  <div id="vscroll"><div class="sthumb" id="vthumb"></div></div>
</div>
<div id="obsbar">
  <div class="obsfield" style="flex:1; min-width:240px"><label>OBS browser source URL</label>
    <div class="urlrow"><input id="obsurl" readonly><button id="copyurl" class="fitb">Copy</button></div></div>
  <div class="obsfield"><label>OBS source (scales to fit any size)</label>
    <div class="canvrow"><span id="canvdim">—</span><button id="copysize" class="fitb" style="margin-left:10px">Copy</button></div></div>
  <div class="obsfield"><label>Playback</label>
    <div class="transport">
      <button class="tbtn" id="t_prev" title="Previous"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="2.6" height="12" rx="1"/><path d="M19 6 10 12l9 6z"/></svg></button>
      <button class="tbtn mid" id="t_play" title="Play / Pause"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"/></svg></button>
      <button class="tbtn" id="t_next" title="Next"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 6l9 6-9 6z"/><rect x="15.4" y="6" width="2.6" height="12" rx="1"/></svg></button>
    </div></div>
</div>
<div id="presetbar">
  <div class="obsfield" style="flex:1; min-width:200px"><label>Preset</label>
    <select id="preset_sel"><option>Default</option></select></div>
  <div class="presetbtns">
    <button class="fitb" id="preset_save">Save</button>
    <button class="fitb" id="preset_saveas">Save as new…</button>
    <button class="fitb" id="preset_del">Delete</button>
  </div>
</div>
</div>
<div id="dock">
  <div class="dpanel"><div class="dhead static">Layers</div>
    <div class="dbody"><div id="opwrap"><span class="oplbl">Opacity</span><input type=range id="oprange" min=0 max=100><span id="opval">100%</span></div>
      <div id="layers"></div>
      <div class="propsep">Properties</div>
      <div class="prophint" id="prophint">Select a layer to edit its properties.</div>
      <div class="fld" data-for="bg"><label>Background</label><select id="bgmode">
        <option value="solid">Solid</option><option value="gradient">Gradient</option><option value="auto">Auto (from cover)</option><option value="autograd">Auto gradient</option><option value="cover">Cover (blurred)</option><option value="none">None</option></select></div>
      <div class="fld" data-for="bg"><label>Card colour</label><input type=color id="bg_color"></div>
      <div class="fld gradfld" data-for="bg"><label>Gradient colour 2</label><input type=color id="bg_color2"></div>
      <div class="fld gradfld" data-for="bg"><label>Gradient angle</label><input type=range id="bg_grad_angle" min=0 max=360></div>
      <div class="fld" data-for="bg"><label>Card corners</label><input type=range id="bg_radius" min=0 max=200></div>
      <div class="fld" data-for="cover"><label>Cover roundness</label><input type=range id="cover_radius" min=0 max=100></div>
      <div class="fld" data-for="cover"><label>Cover filter</label><select id="cover_filter">
        <option value="none">None</option><option value="grey">Greyscale</option><option value="sepia">Sepia (mono)</option></select></div>
      <div class="fld" data-for="text"><label>Title colour</label><input type=color id="text_color"></div>
      <div class="fld" data-for="text"><label>Artist colour</label><input type=color id="sub_color"></div>
      <div class="fld" data-for="text"><label>Text align</label>
        <div class="seg" id="text_align">
          <button data-v="left" title="Left"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="3" y="11" width="11" height="2.4" rx="1"/><rect x="3" y="17" width="15" height="2.4" rx="1"/></svg></button>
          <button data-v="center" title="Center"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="6.5" y="11" width="11" height="2.4" rx="1"/><rect x="4.5" y="17" width="15" height="2.4" rx="1"/></svg></button>
          <button data-v="right" title="Right"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="10" y="11" width="11" height="2.4" rx="1"/><rect x="6" y="17" width="15" height="2.4" rx="1"/></svg></button>
        </div></div>
      <div class="fld" data-for="time"><label>Counter align</label>
        <div class="seg" id="time_align">
          <button data-v="left" title="Left"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="3" y="11" width="11" height="2.4" rx="1"/><rect x="3" y="17" width="15" height="2.4" rx="1"/></svg></button>
          <button data-v="center" title="Center"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="6.5" y="11" width="11" height="2.4" rx="1"/><rect x="4.5" y="17" width="15" height="2.4" rx="1"/></svg></button>
          <button data-v="right" title="Right"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="5" width="18" height="2.4" rx="1"/><rect x="10" y="11" width="11" height="2.4" rx="1"/><rect x="6" y="17" width="15" height="2.4" rx="1"/></svg></button>
        </div></div>
      <div class="fld" data-for="video"><label>Video URL</label><input type=text id="video_url" placeholder="https://… .mp4  (Spotify Canvas auto-resolves later)" style="width:100%;box-sizing:border-box;background:#2b2b29;color:#f0f0f0;border:none;border-radius:6px;padding:8px 9px;font-size:12px"></div>
      <div class="fld" data-for="video"><label>Video fit</label><select id="video_fit"><option value="cover">Cover (crop)</option><option value="contain">Contain (fit)</option><option value="fill">Fill (stretch)</option></select></div>
      <div class="fld" data-for="video"><label>Roundness</label><input type=range id="video_radius" min=0 max=120></div>
      <div class="propsep" data-for="video" style="font-size:10.5px;color:#8a8a88;font-weight:400;border:none;padding-top:0">Paste a looping mp4 to test. Auto Canvas/Apple video comes later. Hidden when empty.</div>
      <div class="fld" data-for="bars"><label>Bars colour</label><input type=color id="accent"></div>
      <div class="fld" data-for="prog"><label>Progress colour</label><input type=color id="prog_color"></div>
      <label class="ac" data-for="prog" style="margin:0; justify-content:flex-start"><input type=checkbox id="prog_auto"> Auto colour (from cover)</label>
      <div class="fld" data-for="vol"><label>Volume colour</label><input type=color id="vol_color"></div>
      <div class="fld" data-for="vol"><label>Volume style</label>
        <div class="seg" id="vol_style">
          <button data-v="arcs" title="Speaker + sound waves">Arcs</button>
          <button data-v="bar" title="Speaker + level line">Bar</button>
        </div></div>
      <label class="ac" data-for="bars" style="margin:0; justify-content:flex-start"><input type=checkbox id="accent_auto"> Auto colour (from cover)</label>
      <div class="fld" data-for="bars"><label>EQ bars · count</label><input type=range id="bars_n" min=3 max=12></div>
      <div class="fld" data-for="bars"><label>EQ bars · gap</label><input type=range id="bars_gap" min=10 max=80></div>
      <label class="ac" data-for="bars" style="margin:0; justify-content:flex-start"><input type=checkbox id="bars_round"> Rounded bars</label>
      <label class="ac" data-for="bars" style="margin:0; justify-content:flex-start"><input type=checkbox id="bars_glow"> Bar glow</label>
      <label class="ac" data-for="bars" style="margin:0; justify-content:flex-start"><input type=checkbox id="bars_reactive"> Reactive (follow the audio)</label>
      <label class="ac" data-for="bars" style="margin:0; justify-content:flex-start"><input type=checkbox id="bars_attach"> Bars follow text</label>
      <div id="fxbody"><div id="fxpophead"><span id="fxpoptitle">Effects</span><span id="fxpopx" title="Close">&times;</span></div>
      <div class="fxcard" data-for="bg cover text bars prog vol time">
        <label class="fxtog"><input type=checkbox id="fx_sh"> Drop shadow</label>
        <div class="fxgroup" id="fxg_sh">
          <div class="fld"><label>Colour</label><input type=color id="fx_shc"></div>
          <div class="fld"><label>Softness</label><input type=range id="fx_shb" min=0 max=40 step="any"></div>
          <div class="fld"><label>Opacity</label><input type=range id="fx_sho" min=0 max=100 step="any"></div>
          <div class="fld"><label>Angle</label><div class="angwheel" id="fx_sha"><div class="anghand"></div></div></div>
          <div class="fld"><label>Distance</label><input type=range id="fx_shd" min=0 max=30 step="any"></div>
        </div>
      </div>
      <div class="fxcard" data-for="bg cover text bars prog vol time">
        <label class="fxtog"><input type=checkbox id="fx_ol"> Outline</label>
        <div class="fxgroup" id="fxg_ol">
          <label class="ac" style="margin:0; justify-content:flex-start; font-size:11px"><input type=checkbox id="fx_ola"> Auto colour (from cover)</label>
          <div class="fld"><label>Colour</label><input type=color id="fx_olc"></div>
          <div class="fld"><label>Width</label><input type=range id="fx_olw" min=0 max=8 step="any"></div>
        </div>
      </div>
      </div>
    </div></div>
</div>
</div>
</div>
<script>
let P={}, saveT=null, _barLv=null, _vol=1, ZOOM=2, panX=0, panY=0;   // editor zoom + pan (Alt+scroll = zoom; scroll/middle-drag = pan)
const NOART="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='%23ffffff' fill-opacity='0.28' d='M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z'/></svg>";
function hexA(hex,a){ const m=/^#?([0-9a-f]{6})$/i.exec(hex||''); if(!m) return hex||'';
  const n=parseInt(m[1],16); return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`; }
function el(id){ return document.getElementById(id); }
let _autoRGB=null, _clipW=null, npPos=0, npDur=0, npPlaying=false, npAt=0, _lastClkVer=null, _npVideo='';
function fmt(s){ s=Math.max(0,Math.floor(s||0)); return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2); }
function updProg(){ const t=npPos+(npPlaying?(performance.now()-npAt)/1000:0);
  const f=el('progfill'); if(f) f.style.width=((npDur>0?Math.max(0,Math.min(1,t/npDur)):0)*100)+'%';
  const te=el('time'); if(te){ const _o=te.textContent; te.textContent=fmt(t)+' / '+fmt(npDur); if(_o!==te.textContent && selset.has('time')) reflowSel(); } }   // time text width can change as it ticks -> keep the selection box in sync
setInterval(updProg, 250);
function updFade(c){ const over=c.scrollWidth-c.clientWidth; let m;
  if(over<=2) m='none';
  else { const l=c.scrollLeft>2?'transparent 0,#000 16px':'#000 0',
        r=c.scrollLeft<over-2?'#000 calc(100% - 16px),transparent 100%':'#000 100%';
    m='linear-gradient(to right,'+l+','+r+')'; }
  if(c._mask!==m){ c._mask=m; c.style.webkitMaskImage=m; c.style.maskImage=m; } }
function tweenScroll(c,to,dur){ if(c._tw)cancelAnimationFrame(c._tw); const from=c.scrollLeft, d=to-from; let t0=null;
  function step(ts){ if(t0===null)t0=ts; const k=Math.min(1,(ts-t0)/dur), e=k;   // linear ticker
    c.scrollLeft=from+d*e; if(k<1) c._tw=requestAnimationFrame(step); } c._tw=requestAnimationFrame(step); }
function marquee(cid){ const c=el(cid); if(!c) return; clearInterval(c._mq);
  c.onscroll=()=>updFade(c); c.scrollLeft=0; updFade(c);
  const over=c.scrollWidth-c.clientWidth; if(over<=2) return;
  const dur=Math.min(6000,1200+over*10); let at=0;
  c._mq=setInterval(()=>{ at=1-at; tweenScroll(c, at?over:0, dur); }, dur+5000); }
function sampleArt(img){ try{ const c=document.createElement('canvas'); c.width=c.height=24;
  const x=c.getContext('2d'); x.drawImage(img,0,0,24,24); const d=x.getImageData(0,0,24,24).data;
  let r=0,g=0,b=0,w=0; for(let i=0;i<d.length;i+=4){ const R=d[i],G=d[i+1],B=d[i+2];
    const wt=12+(Math.max(R,G,B)-Math.min(R,G,B)); r+=R*wt; g+=G*wt; b+=B*wt; w+=wt; }
  return [Math.round(r/w),Math.round(g/w),Math.round(b/w)]; }catch(e){ return null; } }
function bgCss(p){ const op=p.bg_opacity==null?0.85:p.bg_opacity;
  if(p.bg_grad_user){ return 'linear-gradient('+(p.bg_grad_angle==null?135:p.bg_grad_angle)+'deg, '+hexA(p.bg_color||'#2b2b29',op)+', '+hexA(p.bg_color2||'#141414',op)+')'; }   // user gradient: pick 2 colours + angle
  if(p.bg_auto && _autoRGB){ const a=_autoRGB;
    if(p.bg_grad){ const c1='rgba('+Math.round(a[0]*.72)+','+Math.round(a[1]*.72)+','+Math.round(a[2]*.72)+','+op+')',
                       c2='rgba('+Math.round(a[0]*.28)+','+Math.round(a[1]*.28)+','+Math.round(a[2]*.28)+','+op+')';
      return 'linear-gradient(135deg, '+c1+', '+c2+')'; }
    const f=0.55; return 'rgba('+Math.round(a[0]*f)+','+Math.round(a[1]*f)+','+Math.round(a[2]*f)+','+op+')'; }
  return hexA(p.bg_color||'#2b2b29', op); }
function accentCss(p){ return (p.accent_auto && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (p.accent||'#fff'); }
function progCss(p){ return (p.prog_auto && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (p.prog_color||p.accent||'#fff'); }   // base accent, NOT accentCss -> bars auto-colour doesn't bleed into the progress bar
function volCss(p){ return p.vol_color||p.accent||'#fff'; }   // base accent, NOT accentCss -> bars auto-colour doesn't bleed into the volume meter
// Volume layer: speaker glyph + sound-wave arcs that light up with loudness (mean of the EQ bands).
(function(){ const cv=document.getElementById('vol'); if(!cv||!cv.getContext) return;
  const ctx=cv.getContext('2d'); let lvl=0;
  function rr(x,y,ww,hh,r){ ctx.beginPath(); if(ctx.roundRect) ctx.roundRect(x,y,ww,hh,r); else ctx.rect(x,y,ww,hh); ctx.fill(); }
  function draw(){ const d=window.devicePixelRatio||1, rect=cv.getBoundingClientRect();
    const wW=Math.max(2,Math.round(rect.width*d)), wH=Math.max(2,Math.round(rect.height*d));
    if(cv.width!==wW) cv.width=wW; if(cv.height!==wH) cv.height=wH;
    const w=cv.width, h=cv.height; ctx.clearRect(0,0,w,h);
    const col=(getComputedStyle(cv).getPropertyValue('--vol')||getComputedStyle(cv).getPropertyValue('--accent')||'#fff').trim();
    const tgt=Math.max(0,Math.min(1,(_vol==null?0.6:_vol))); lvl+=(tgt-lvl)*0.25;   // SET volume level (not loudness)
    ctx.fillStyle=col; ctx.strokeStyle=col; ctx.lineCap='round'; ctx.lineJoin='round';
    const bar=!!(P&&P.vol_style==='bar');
    if(bar){   // box-relative: speaker + waves sized to box height (left); level bar fills the rest of the width -> widen the layer to lengthen the bar
      const sy=h/2, sx=h*0.12, bw=h*0.13, bh=h*0.28;
      ctx.lineWidth=Math.max(1.5,h*0.07);
      ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.5); ctx.lineTo(sx+bw,sy-bh*0.5); ctx.lineTo(sx+bw*2.1,sy-bh);
        ctx.lineTo(sx+bw*2.1,sy+bh); ctx.lineTo(sx+bw,sy+bh*0.5); ctx.lineTo(sx,sy+bh*0.5); ctx.closePath();
      ctx.fill(); ctx.stroke();
      if(lvl<=0.01){ ctx.save(); ctx.lineCap='round';   // muted -> slash over the speaker, no waves/bar
        ctx.globalCompositeOperation='destination-out'; ctx.lineWidth=h*0.12; ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
        ctx.globalCompositeOperation='source-over'; ctx.lineWidth=Math.max(1.5,h*0.055); ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
        ctx.restore(); requestAnimationFrame(draw); return; }
      const ax=sx+bw*2.6; ctx.lineWidth=Math.max(1.5,h*0.05);
      for(let i=0;i<2;i++){ const r=h*(0.15+i*0.11); ctx.globalAlpha=lvl>(i+0.2)/2?0.9:0.18;
        ctx.beginPath(); ctx.arc(ax,sy,r,-Math.PI/4.5,Math.PI/4.5); ctx.stroke(); }
      ctx.globalAlpha=1;
      const tx0=ax+h*0.60, tx1=w-h*0.10, tw=Math.max(h*0.18,tx1-tx0), th=h*0.16, ry=th/2;
      ctx.globalAlpha=0.22; rr(tx0,sy-ry,tw,th,ry);
      ctx.globalAlpha=1; rr(tx0,sy-ry,Math.max(th,tw*lvl),th,ry);
      requestAnimationFrame(draw); return;
    }
    const CL=0.02, CW=1.06, CH=0.80, PAD=0.94;   // arcs: uniform-fit content bbox (incl. arc reach ax+r + stroke)
    const u=Math.min(w*PAD/CW, h*PAD/CH), sy=0.5*u; ctx.save(); ctx.translate(w/2-(CL+CW/2)*u, h/2-0.5*u);
    const bw=u*0.16, bh=u*0.30, sx=u*0.10;
    ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.5); ctx.lineTo(sx+bw,sy-bh*0.5); ctx.lineTo(sx+bw*2.1,sy-bh);
      ctx.lineTo(sx+bw*2.1,sy+bh); ctx.lineTo(sx+bw,sy+bh*0.5); ctx.lineTo(sx,sy+bh*0.5); ctx.closePath();
    ctx.lineWidth=Math.max(1.5,u*0.14); ctx.fill(); ctx.stroke();
    if(lvl<=0.01){ ctx.save(); ctx.lineCap='round';   // muted -> slash over the speaker, no waves
      ctx.globalCompositeOperation='destination-out'; ctx.lineWidth=u*0.12; ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
      ctx.globalCompositeOperation='source-over'; ctx.lineWidth=Math.max(1.5,u*0.055); ctx.beginPath(); ctx.moveTo(sx,sy-bh*0.95); ctx.lineTo(sx+bw*2.5,sy+bh*0.95); ctx.stroke();
      ctx.restore(); ctx.restore(); requestAnimationFrame(draw); return; }
    const ax=sx+bw*2.5; ctx.lineWidth=Math.max(1.5,u*0.06);
    for(let i=0;i<3;i++){ const on=lvl>(i+0.15)/3, r=u*(0.20+i*0.17);
      ctx.globalAlpha=on?Math.min(1,0.45+lvl*0.55):0.13;
      ctx.beginPath(); ctx.arc(ax,sy,r,-Math.PI/4.5,Math.PI/4.5); ctx.stroke(); }
    ctx.globalAlpha=1; ctx.restore(); requestAnimationFrame(draw); }
  requestAnimationFrame(draw); })();
// EQ bars on a canvas: backing tracks displayed device px (incl. preview scale) -> crisp + uniform, cleared each frame.
(function(){
  const cv=document.getElementById('bars'); if(!cv||!cv.getContext) return;
  const ctx=cv.getContext('2d');
  const ph=[0,1.7,3.1,0.8,2.3], sp=[5.5,7.2,4.6,7.8,6.2], lo=[.42,.52,.38,.58,.48], hi=[1,.82,.95,1,.74];
  let disp=[], t0=null;
  function grp(arr,i,N){ const M=arr.length, a=Math.floor(i*M/N), b=Math.max(a+1,Math.floor((i+1)*M/N)); let s=0; for(let j=a;j<b;j++) s+=arr[j]||0; return s/(b-a); }
  function draw(ts){ if(t0===null)t0=ts; const t=(ts-t0)/1000, d=window.devicePixelRatio||1;
    const rect=cv.getBoundingClientRect();
    const wW=Math.max(2,Math.round(rect.width*d)), wH=Math.max(2,Math.round(rect.height*d));
    if(cv.width!==wW) cv.width=wW; if(cv.height!==wH) cv.height=wH;
    const h=cv.height, N=Math.max(3,Math.min(12,(P.bars_n||5)));
    if(disp.length!==N) disp=new Array(N).fill(0.5);
    const gap=(P.bars_gap==null?0.4:P.bars_gap), round=P.bars_round!==false, glow=P.bars_glow===true;
    const pitch=cv.width/N, GAPd=Math.max(0,Math.round(pitch*gap)), BWd=Math.max(1,Math.round(pitch-GAPd));
    ctx.clearRect(0,0,cv.width,h);
    const col=(getComputedStyle(cv).getPropertyValue('--accent')||'#fff').trim();
    ctx.fillStyle=col; ctx.shadowBlur=glow?Math.max(4,BWd*0.9):0; ctx.shadowColor=glow?col:'transparent';
    const useLv = _barLv && (P.bars_reactive!==false);
    for(let i=0;i<N;i++){
      const tgt = useLv ? Math.max(0,Math.min(1,grp(_barLv,i,N)))                               // real music (12 bands -> N)
                        : lo[i%5]+(hi[i%5]-lo[i%5])*(0.5+0.5*Math.sin(t*sp[i%5]+ph[i%5]));
      disp[i] += (tgt-disp[i]) * (useLv?0.55:0.18);
      const bh=Math.max(BWd,disp[i]*h), x=Math.round(i*pitch+GAPd/2), y=h-bh, r=round?Math.min(BWd/2,bh/2):0;
      ctx.beginPath();
      if(round&&ctx.roundRect){ ctx.roundRect(x,y,BWd,bh,[r,r,r,r]); } else { ctx.rect(x,y,BWd,bh); }
      ctx.fill();
    }
    ctx.shadowBlur=0;
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();
function pos(id,b){ const e=el(id); if(!e||!b) return;
  if(b.x!=null) e.style.left=b.x+'px'; if(b.y!=null) e.style.top=b.y+'px';
  if(b.w!=null) e.style.width=b.w+'px'; if(b.h!=null) e.style.height=b.h+'px'; }
function placeArt(clip,cov,bg){ const art=el('art'), bgEl=el('bg'), root=el('cv');
  if(clip){ if(art.parentNode!==bgEl) bgEl.appendChild(art);
    art.style.left=(cov.x-bg.x)+'px'; art.style.top=(cov.y-bg.y)+'px'; }
  else { if(art.parentNode!==root) root.insertBefore(art, el('meta'));
    art.style.left=cov.x+'px'; art.style.top=cov.y+'px'; }
  art.style.width=cov.size+'px'; art.style.height=cov.size+'px'; }
function placeBars(attach){ const b=el('bars'), ar=el('artist'), root=el('cv'); const vis=P.show_bars!==false;
  if(attach){ if(b.parentNode!==ar) ar.appendChild(b); b.style.position='static'; b.style.left=''; b.style.top='';
    b.style.display=vis?'inline-block':'none'; b.style.verticalAlign='middle'; b.style.width='23px'; b.style.height='11px'; }
  else { if(b.parentNode!==root) root.appendChild(b); b.style.position='absolute'; b.style.display=vis?'block':'none'; } }
function L(){ if(!P.layout) P.layout={cw:300,ch:240,bg:{x:0,y:0,w:252,h:84},
    cover:{x:12,y:12,size:60},text:{x:84,y:20,scale:1},bars:{x:150,y:40,w:40,h:14},prog:{x:84,y:74,w:150},vol:{x:196,y:92,w:56,h:26}};
  if(!P.layout.prog) P.layout.prog={x:84,y:74,w:150,h:4}; if(P.layout.prog.h==null) P.layout.prog.h=4;   // older presets lack prog / prog height
  if(!P.layout.bars) P.layout.bars={x:150,y:40,w:40,h:14};       // ...and the bars layer
  if(!P.layout.vol) P.layout.vol={x:196,y:92,w:56,h:26};         // ...and the volume meter
  if(!P.layout.time) P.layout.time={x:84,y:92,w:96,h:16};        // ...and the song-time counter
  if(!P.layout.video) P.layout.video={x:0,y:0,w:252,h:84};       // ...and the Canvas/video backdrop
  if(!P.layout.order) P.layout.order=['bg','video','cover','text','bars','prog','vol','time'];
  return P.layout; }
function order(){ const lo=L();   // z-order array, ensure every part present
  for(const p of ['bg','cover','text','bars','prog','vol','time','video']) if(lo.order.indexOf(p)<0) lo.order.push(p);
  return lo.order; }
const LBL={bg:'Background',cover:'Cover',text:'Text',bars:'Bars',prog:'Progress',vol:'Volume',time:'Time',video:'Video'};
function clearDrop(){ document.querySelectorAll('.lrow.over-top,.lrow.over-bottom').forEach(r=>r.classList.remove('over-top','over-bottom')); }
function layerVisible(p){ if(p==='bg') return P.bg_enabled!==false; if(p==='cover') return P.show_art!==false;
  if(p==='text') return P.show_title!==false||P.show_artist!==false; if(p==='bars') return P.show_bars!==false;
  if(p==='prog') return P.show_prog!==false; if(p==='vol') return P.show_vol===true; if(p==='time') return P.show_time===true; if(p==='video') return P.show_video===true; return true; }
function toggleLayer(p){ const v=layerVisible(p);
  if(p==='bg') P.bg_enabled=!v; else if(p==='cover') P.show_art=!v;
  else if(p==='text'){ P.show_title=!v; P.show_artist=!v; }
  else if(p==='bars') P.show_bars=!v; else if(p==='prog') P.show_prog=!v; else if(p==='vol') P.show_vol=!v; else if(p==='time') P.show_time=!v; else if(p==='video') P.show_video=!v; }
function EYE(on){ return on
  ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>'
  : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18"/><path d="M10.6 5.1A11 11 0 0 1 12 5c6.5 0 10 7 10 7a13.6 13.6 0 0 1-2.3 3M6.5 6.6A13.4 13.4 0 0 0 2 12s3.5 7 10 7a10.7 10.7 0 0 0 4-.8"/></svg>'; }
const CLIPICON='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5v7a3 3 0 0 0 3 3h7"/><path d="M15 11l4 4-4 4"/></svg>';
const LOCKICON='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
const UNLOCKICON='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.5-1.9"/></svg>';
function layerThumb(p){
  if(p==='cover'){ const a=el('art'), s=(a&&a.getAttribute('src'))||''; return s?'<img src="'+s+'">':''; }
  if(p==='bg'){ return '<div style="width:100%;height:100%;background:'+bgCss(P)+'"></div>'; }
  if(p==='text'){ return '<span style="color:'+(P.text_color||'#fff')+';font-weight:700;font-size:13px">Aa</span>'; }
  if(p==='bars'){ return '<div class="tbars" style="color:'+accentCss(P)+'"><i></i><i></i><i></i></div>'; }
  if(p==='prog'){ return '<div style="width:72%;height:3px;border-radius:2px;background:'+progCss(P)+'"></div>'; }
  if(p==='vol'){ return '<svg width="18" height="18" viewBox="0 0 24 24" fill="'+volCss(P)+'"><path d="M4 9v6h4l5 4V5L8 9H4z"/></svg>'; }
  if(p==='time'){ return '<span style="color:'+(P.sub_color||'#c8c8d0')+';font-size:9px;font-weight:600;font-variant-numeric:tabular-nums">0:00</span>'; }
  if(p==='video'){ return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#cfcfcc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="14" height="14" rx="2"/><path d="M21 8l-4 4 4 4z"/></svg>'; }
  return ''; }
function buildLayers(){ const box=el('layers'); if(!box) return; box.innerHTML='';
  const o=order(), vis=o.slice().reverse();   // front -> back (top of list = front)
  const baseOf={};   // a layer is a "mask base" if some clipped layer above resolves down to it
  o.forEach((p,i)=>{ if(P.clip&&P.clip[p]){ const b=clipBase(o,i); if(b>=0) baseOf[o[b]]=true; } });
  vis.forEach((p)=>{ const clipped=!!(P.clip&&P.clip[p]);
    const row=document.createElement('div'); row.className='lrow'+(clipped?' clipped':'')+(selset.has(p)?' sel':''); row.draggable=true; row.dataset.part=p;
    row.addEventListener('click', e=>{ if(e.target.closest('.leye,.lclip,.lfx')) return; selset.clear(); selset.add(p); pinHandles(); });
    if(clipped){ const ca=document.createElement('span'); ca.className='carrow'; ca.innerHTML='<svg width="13" height="26" viewBox="0 0 13 26" fill="none" stroke="#FF7A1A" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5H5v13"/><path d="M2.5 15 5 19l2.5-4"/></svg>'; row.appendChild(ca); }
    const ev=layerVisible(p); const eye=document.createElement('span'); eye.className='leye'+(ev?'':' off'); eye.innerHTML=EYE(ev); eye.title='Show / hide layer';
    eye.addEventListener('mousedown', e=>e.stopPropagation());
    eye.addEventListener('click', e=>{ e.stopPropagation(); beginUndo(); toggleLayer(p); commitUndo(); applyCanvas(); syncControls(); save(); buildLayers(); });
    row.appendChild(eye);
    const th=document.createElement('span'); th.className='lthumb'; th.innerHTML=layerThumb(p); row.appendChild(th);
    const nm=document.createElement('span'); nm.className='lname'+(baseOf[p]?' base':''); nm.textContent=LBL[p]||p; row.appendChild(nm);
    if(p!==o[0]){ const cb=document.createElement('span'); cb.className='lclip'+(clipped?' on':''); cb.textContent=clipped?'Clipped':'Clip';
      cb.title='Clip to the layer below (mask). Underlined layer = the mask.';
      cb.addEventListener('mousedown', e=>e.stopPropagation());
      cb.addEventListener('click', e=>{ e.stopPropagation(); beginUndo(); if(!P.clip)P.clip={}; P.clip[p]=!P.clip[p]; commitUndo(); applyCanvas(); save(); buildLayers(); });
      row.appendChild(cb); }
    { const fxb=document.createElement('span'); fxb.className='lfx'+((P.fx&&P.fx[p]&&(P.fx[p].sh||P.fx[p].ol))?' on':''); fxb.textContent='FX'; fxb.title='Effects (drop shadow / outline)';
      fxb.addEventListener('mousedown', e=>e.stopPropagation());
      fxb.addEventListener('click', e=>{ e.stopPropagation(); openFxPop(p, fxb); }); row.appendChild(fxb); }
    const lk=document.createElement('span'); lk.className='llock'+(locked(p)?' on':''); lk.innerHTML=locked(p)?LOCKICON:UNLOCKICON;
    lk.title='Lock layer (no move / resize)';
    lk.addEventListener('mousedown', e=>e.stopPropagation());
    lk.addEventListener('click', e=>{ e.stopPropagation(); beginUndo(); if(!P.lock)P.lock={}; P.lock[p]=!locked(p); commitUndo(); applyCanvas(); save(); buildLayers(); });
    row.insertBefore(lk, row.firstChild);   // lock furthest left
    row.addEventListener('dragstart', e=>{ if(e.target.closest('.leye,.lclip,.llock,.lfx')){ e.preventDefault(); return; } row.classList.add('drag'); e.dataTransfer.setData('text', p); e.dataTransfer.effectAllowed='move'; });
    row.addEventListener('dragend', ()=>{ row.classList.remove('drag'); clearDrop(); });
    row.addEventListener('dragover', e=>{ e.preventDefault(); const r=row.getBoundingClientRect(), below=(e.clientY-r.top)>r.height/2;
      clearDrop(); row.classList.add(below?'over-bottom':'over-top'); });
    row.addEventListener('drop', e=>{ e.preventDefault(); const r=row.getBoundingClientRect(), below=(e.clientY-r.top)>r.height/2;
      clearDrop(); const from=e.dataTransfer.getData('text'), to=p; if(from===to) return;
      beginUndo(); const v=order().slice().reverse(); v.splice(v.indexOf(from),1);
      const ti=v.indexOf(to); v.splice(below?ti+1:ti, 0, from); P.layout.order=v.reverse();
      commitUndo(); applyCanvas(); save(); buildLayers(); });
    box.appendChild(row); }); }
function drawOutlines(set){ const s=el('stage').getBoundingClientRect();
  ['bg','cover','text','bars','prog','vol','time','video'].forEach(p=>{ const o=outlines[p], on=set.has(p)&&visible(p);   // outline in #stage (not clipped by bg)
    if(!on){ o.style.display='none'; return; }
    const r=el(partEl(p)).getBoundingClientRect(); o.style.display='block';
    o.style.left=(r.left-s.left)+'px'; o.style.top=(r.top-s.top)+'px'; o.style.width=r.width+'px'; o.style.height=r.height+'px'; }); }
function reflowSel(){ const s=el('stage').getBoundingClientRect();   // cheap geometry redraw (outline boxes + handles) - safe to call on every tick
  drawOutlines(selset);
  const single=selset.size===1;   // resize handles only for a single selection
  handles.forEach(H=>{ const on=single && selset.has(H.part) && visible(H.part) && !locked(H.part);
    if(!on){ H.node.classList.remove('on'); return; }
    const r=el(H.elid).getBoundingClientRect(), d=H.corner; H.node.classList.add('on');
    const lf=r.left-s.left, rt=r.right-s.left, tp=r.top-s.top, bt=r.bottom-s.top;
    const x=d.includes('w')?lf:d.includes('e')?rt:(lf+rt)/2;
    const y=d.includes('n')?tp:d.includes('s')?bt:(tp+bt)/2;
    H.node.style.left=(x-6)+'px'; H.node.style.top=(y-6)+'px'; });
  const rone=single?[...selset][0]:null;   // corner-radius drag handles (bg/cover), one per corner
  if((rone==='bg'||rone==='cover'||rone==='video') && visible(rone) && !locked(rone)){ const box=boxOf(rone), rc=el(partEl(rone)).getBoundingClientRect();
    const rcv = rone==='bg' ? Math.min((P.bg_radius==null?13:P.bg_radius), Math.min(box.w,box.h)/2) : rone==='video' ? Math.min((P.video_radius||0), Math.min(box.w,box.h)/2) : coverRad()/100*Math.min(box.w,box.h)/2;
    const ins=Math.max(7,rcv*ZOOM*0.5), Lx=rc.left-s.left, Tp=rc.top-s.top, Rx=rc.right-s.left, Bt=rc.bottom-s.top;
    const P4={nw:[Lx+ins,Tp+ins], ne:[Rx-ins,Tp+ins], sw:[Lx+ins,Bt-ins], se:[Rx-ins,Bt-ins]};
    radhs.forEach(rh=>{ const q=P4[rh.dataset.corner]; rh.style.display='block'; rh.style.left=(q[0]-6)+'px'; rh.style.top=(q[1]-6)+'px'; }); }
  else radhs.forEach(rh=>rh.style.display='none');
  const gm=[...selset].filter(p=>visible(p)&&!locked(p));   // group-resize handles around the multi-selection bbox
  if(gm.length>=2){ let Lx=1e9,Tp=1e9,Rx=-1e9,Bt=-1e9; gm.forEach(p=>{ const r=el(partEl(p)).getBoundingClientRect(); Lx=Math.min(Lx,r.left); Tp=Math.min(Tp,r.top); Rx=Math.max(Rx,r.right); Bt=Math.max(Bt,r.bottom); });
    ghandles.forEach(g=>{ const d=g.corner; g.node.classList.add('on'); g.node.style.left=((d.includes('w')?Lx:Rx)-s.left-6)+'px'; g.node.style.top=((d.includes('n')?Tp:Bt)-s.top-6)+'px'; }); }
  else ghandles.forEach(g=>g.node.classList.remove('on')); }
function pinHandles(){ reflowSel(); refreshSel(); updCanvDim(); }   // full re-pin = geometry + property sync (on selection change / drag)
function panRange(){ const stage=el('stage'), lo=L(), MIN=Math.min(stage.clientWidth,stage.clientHeight)*0.6;   // always allow some panning, even zoomed out
  return { x:Math.max(lo.cw*ZOOM-stage.clientWidth, MIN), y:Math.max(lo.ch*ZOOM-stage.clientHeight, MIN), vpW:stage.clientWidth, vpH:stage.clientHeight }; }
function updateScroll(){   // clamp pan, apply the transform, size + place the always-on scrollbar thumbs
  const R=panRange(); panX=Math.max(-R.x/2,Math.min(R.x/2,panX)); panY=Math.max(-R.y/2,Math.min(R.y/2,panY));
  el('cv').style.transform='translate('+panX+'px,'+panY+'px) scale('+ZOOM+')';
  const hs=el('hscroll'), vs=el('vscroll'), ht=el('hthumb'), vt=el('vthumb'); if(!hs) return;
  const trX=hs.clientWidth, fw=Math.max(28,(R.vpW/(R.vpW+R.x))*trX), tX=Math.min(1,Math.max(0,0.5-panX/R.x)); ht.style.width=fw+'px'; ht.style.left=(tX*(trX-fw))+'px';
  const trY=vs.clientHeight, fh=Math.max(28,(R.vpH/(R.vpH+R.y))*trY), tY=Math.min(1,Math.max(0,0.5-panY/R.y)); vt.style.height=fh+'px'; vt.style.top=(tY*(trY-fh))+'px';
}
function dragScroll(thumb, axis){ thumb.addEventListener('mousedown', e=>{ e.preventDefault(); e.stopPropagation();
  const sc=axis==='x'?e.clientX:e.clientY, p0=axis==='x'?panX:panY, R=panRange(), range=axis==='x'?R.x:R.y;
  const tr=axis==='x'?el('hscroll').clientWidth:el('vscroll').clientHeight, tl=axis==='x'?thumb.offsetWidth:thumb.offsetHeight, t0=0.5-p0/range;
  const move=ev=>{ const d=(axis==='x'?ev.clientX:ev.clientY)-sc, t=Math.min(1,Math.max(0,t0+d/(tr-tl))), np=range*(0.5-t);
    if(axis==='x') panX=np; else panY=np; updateScroll(); pinHandles(); };
  const up=()=>{ window.removeEventListener('mousemove',move); window.removeEventListener('mouseup',up); };
  window.addEventListener('mousemove',move); window.addEventListener('mouseup',up); }); }
function applyCanvas(){
  const cv=el('cv'), lo=L();
  cv.style.transform='translate('+panX+'px,'+panY+'px) scale('+ZOOM+')';   // editor zoom + pan
  updateScroll();
  if(!P.clip) P.clip = {};   // per-layer "clip to below" map
  cv.style.setProperty('--text', P.text_color||'#fff');
  cv.style.setProperty('--sub', P.sub_color||'#c8c8d0');
  cv.style.setProperty('--accent', accentCss(P));
  cv.style.setProperty('--prog', progCss(P));
  cv.style.setProperty('--vol', volCss(P));
  if((P.bg_auto||P.accent_auto||P.prog_auto||anyFxAuto(P)) && !_autoRGB){ const A=el('art'); if(A.complete&&A.naturalWidth) _autoRGB=sampleArt(A); }
  cv.style.setProperty('--bg', bgCss(P));
  cv.style.setProperty('--bg-radius', (P.bg_radius==null?13:P.bg_radius)+'px');
  cv.style.setProperty('--art-radius', (coverRad()*0.5)+'%');
  ['bg','cover','text','bars','prog','vol','time','video'].forEach(pt=>{ const e=el(partEl(pt)); if(!e) return; const base=pt==='cover'?artFilter(P):''; const all=[(base&&base!=='none')?base:'', fxFilter(P,pt)].filter(Boolean).join(' '); e.style.filter=all||'none'; e.style.boxShadow=fxBoxShadow(P,pt)||''; });
  cv.classList.toggle('nobg', P.bg_enabled===false);
  cv.style.width=lo.cw+'px'; cv.style.height=lo.ch+'px';
  el('art').classList.toggle('hidden', P.show_art===false);
  el('title').classList.toggle('hidden', P.show_title===false);
  el('artistname').classList.toggle('hidden', P.show_artist===false);
  el('bars').classList.toggle('hidden', P.show_bars===false);
  el('prog').classList.toggle('hidden', P.show_prog===false);
  el('vol').classList.toggle('hidden', P.show_vol!==true);
  el('time').classList.toggle('hidden', P.show_time!==true);
  el('coverbg').classList.toggle('on', P.cover_bg===true && P.bg_enabled!==false);
  pos('bg', lo.bg); pos('art', {x:lo.cover.x,y:lo.cover.y,w:lo.cover.size,h:lo.cover.size}); pos('meta', lo.text); pos('prog', {x:lo.prog.x,y:lo.prog.y,w:lo.prog.w,h:lo.prog.h}); pos('vol', lo.vol); pos('time', lo.time); { const te=el('time'); te.style.fontSize=((lo.time.h||16)*0.62)+'px'; te.style.width='auto'; te.style.height='auto'; te.style.transform=(P.time_align==='center'?'translateX(-50%)':P.time_align==='right'?'translateX(-100%)':'none'); }
  pos('vid', lo.video||{x:0,y:0,w:252,h:84}); applyVideo(P, el('vid'));
  const battach=P.bars_attach!==false; placeBars(battach); if(!battach) pos('bars', lo.bars);
  order().forEach((p,i)=>{ const e=el(partEl(p)); if(e){ e.style.zIndex=i; e.style.opacity=(P.op&&P.op[p]!=null)?P.op[p]:1; } });
  ['bg','cover','text','bars','prog','vol','time','video'].forEach(applyClip);
  el('meta').style.transformOrigin='top left'; el('meta').style.transform='scale('+(lo.text.scale||1)+')';
  el('meta').style.alignItems=(P.text_align==='center'?'center':P.text_align==='right'?'flex-end':'flex-start');
  const tsc=lo.text.scale||1, hasTW=lo.text.w!=null; el('meta').style.width=hasTW?(lo.text.w/tsc)+'px':'';   // fixed text-box width (resizable) or auto-fit
  const clip=hasTW?(lo.text.w/tsc):Math.max(40,((lo.bg.x+lo.bg.w)-lo.text.x-14)/tsc);
  el('title').style.maxWidth=clip+'px'; el('artistname').style.maxWidth=Math.max(30,clip-30)+'px';
  if(clip!==_clipW){ _clipW=clip; marquee('title'); marquee('artistname'); }
  pinHandles();
}
function syncControls(){
  el('bgmode').value=P.bg_enabled===false?'none':P.bg_grad_user?'gradient':P.bg_auto?(P.bg_grad?'autograd':'auto'):(P.cover_bg?'cover':'solid');
  el('bg_color').value=P.bg_color||'#2b2b29'; el('bg_color2').value=P.bg_color2||'#141414'; el('bg_grad_angle').value=P.bg_grad_angle==null?135:P.bg_grad_angle; el('video_url').value=P.video_url||''; el('video_fit').value=P.video_fit||'cover'; el('video_radius').value=P.video_radius||0; sliderFill(el('video_radius')); el('text_color').value=P.text_color||'#ffffff';
  el('sub_color').value=P.sub_color||'#c8c8d0'; el('accent').value=P.accent||'#ffffff';
  el('prog_color').value=P.prog_color||P.accent||'#ffffff'; el('vol_color').value=P.vol_color||P.accent||'#ffffff';
  el('cover_radius').value=coverRad(); sliderFill(el('cover_radius'));
  el('bg_radius').value=P.bg_radius==null?13:P.bg_radius; sliderFill(el('bg_radius'));
  el('accent_auto').checked=P.accent_auto===true; el('prog_auto').checked=P.prog_auto===true; el('cover_filter').value=P.cover_filter||'none';
  document.querySelectorAll('#text_align button').forEach(b=>b.classList.toggle('on', b.dataset.v===(P.text_align||'left')));
  document.querySelectorAll('#time_align button').forEach(b=>b.classList.toggle('on', b.dataset.v===(P.time_align||'left')));
  document.querySelectorAll('#vol_style button').forEach(b=>b.classList.toggle('on', b.dataset.v===(P.vol_style||'arcs')));
  el('bars_attach').checked=P.bars_attach!==false; updCanvDim();
  el('bars_n').value=P.bars_n||5; sliderFill(el('bars_n'));
  el('bars_gap').value=Math.round((P.bars_gap==null?0.4:P.bars_gap)*100); sliderFill(el('bars_gap'));
  el('bars_round').checked=P.bars_round!==false; el('bars_glow').checked=P.bars_glow===true; el('bars_reactive').checked=P.bars_reactive!==false;
}
function save(){ el('saved').style.opacity=1; clearTimeout(saveT);
  saveT=setTimeout(()=>{ fetch('/preset',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(P)}).then(()=>setTimeout(()=>el('saved').style.opacity=0,700)).catch(()=>{}); },250); }
// Undo/redo: snapshot P before each gesture/edit; control bursts coalesce via a
// debounced commit; canvas drags commit on mouseup. Ctrl+Z / Ctrl+Shift+Z (or Ctrl+Y).
let undoStack=[], redoStack=[], _pre=null, _commitT=null;
function snap(){ return JSON.stringify(P); }
function beginUndo(){ if(_pre===null) _pre=snap(); }
function commitUndo(){ if(_pre!==null && _pre!==snap()){ undoStack.push(_pre); if(undoStack.length>60) undoStack.shift(); redoStack=[]; } _pre=null; }
function scheduleCommit(){ clearTimeout(_commitT); _commitT=setTimeout(commitUndo, 450); }
function restoreState(){ if(!P.layout) L(); syncControls(); applyCanvas(); buildLayers(); save(); }
function doUndo(){ if(!undoStack.length) return; redoStack.push(snap()); P=JSON.parse(undoStack.pop()); restoreState(); }
function doRedo(){ if(!redoStack.length) return; undoStack.push(snap()); P=JSON.parse(redoStack.pop()); restoreState(); }
window.addEventListener('keydown', e=>{ if(!(e.ctrlKey||e.metaKey)) return; const k=e.key.toLowerCase();
  if(k==='z'&&!e.shiftKey){ e.preventDefault(); doUndo(); }
  else if((k==='z'&&e.shiftKey)||k==='y'){ e.preventDefault(); doRedo(); } });
function change(){ beginUndo(); scheduleCommit(); applyCanvas(); save(); }
el('bgmode').onchange=e=>{ const v=e.target.value; P.bg_enabled=(v!=='none'); P.cover_bg=(v==='cover'); P.bg_auto=(v==='auto'||v==='autograd'); P.bg_grad=(v==='autograd'); P.bg_grad_user=(v==='gradient'); refreshSel(); change(); };
for(const k of ['bg_color','bg_color2','text_color','sub_color','accent','prog_color','vol_color']) el(k).oninput=e=>{ P[k]=e.target.value; change(); };
el('bg_grad_angle').oninput=e=>{ P.bg_grad_angle=+e.target.value; change(); };
el('video_url').oninput=e=>{ P.video_url=e.target.value; change(); };
el('video_fit').onchange=e=>{ P.video_fit=e.target.value; change(); };
el('video_radius').oninput=e=>{ P.video_radius=+e.target.value; sliderFill(e.target); change(); };
el('vid').addEventListener('loadedmetadata', ()=>{ const vd=el('vid'); if(!(P.video_url||'').trim()) return;   // only auto-fit a MANUALLY pasted URL; auto-Canvas (np.video) keeps your layout on every song
  if(!vd.videoWidth||!vd.videoHeight||vd._sized===vd.currentSrc) return; vd._sized=vd.currentSrc;   // new manual video -> size the box to its native aspect once
  const lo=L(), v=lo.video, ar=vd.videoWidth/vd.videoHeight, cx=v.x+v.w/2, cy=v.y+v.h/2, h=v.h, w=Math.max(16,Math.round(h*ar));
  v.w=w; v.x=Math.round(cx-w/2); v.y=Math.round(cy-h/2); applyCanvas(); save(); if(selset.has('video')) pinHandles(); });
el('cover_radius').oninput=e=>{ P.cover_radius=+e.target.value; sliderFill(e.target); change(); };
function sliderFill(elm){ const mn=+elm.min||0,mx=+elm.max||100,v=(+elm.value-mn)/(mx-mn)*100;
  elm.style.background='linear-gradient(to right,#f0f0f0 '+v+'%,#3a3a38 '+v+'%)'; }
el('bg_radius').oninput=e=>{ P.bg_radius=+e.target.value; sliderFill(e.target); change(); };
el('accent_auto').onchange=e=>{ P.accent_auto=e.target.checked; change(); };
el('prog_auto').onchange=e=>{ P.prog_auto=e.target.checked; change(); };
function selP(){ return selset.size===1?[...selset][0]:null; }   // per-layer FX (drop shadow / outline) on the selected layer
function fxSet(k,v){ const p=selP(); if(!p) return; beginUndo(); P.fx=P.fx||{}; P.fx[p]=P.fx[p]||{}; P.fx[p][k]=v; commitUndo(); change(); }
function fxSync(){ const on=selset.size===1; const g1=el('fxg_sh'), g2=el('fxg_ol'); if(g1) g1.style.display=(on&&el('fx_sh').checked)?'':'none'; if(g2) g2.style.display=(on&&el('fx_ol').checked)?'':'none'; }   // collapse a card's controls when its effect is off
el('fx_sh').onchange=e=>{ fxSet('sh',e.target.checked); fxSync(); }; el('fx_shc').oninput=e=>fxSet('shc',e.target.value); el('fx_shb').oninput=e=>fxSet('shb',+e.target.value);
el('fx_sho').oninput=e=>fxSet('sho',+e.target.value); el('fx_shd').oninput=e=>fxSet('shd',+e.target.value);
(function(){ const w=el('fx_sha'); if(!w) return;   // angle dial: click/drag -> angle from centre
  function ang(e){ const r=w.getBoundingClientRect(); let a=Math.atan2(e.clientY-(r.top+r.height/2), e.clientX-(r.left+r.width/2))*180/Math.PI; if(a<0)a+=360; return Math.round(a); }
  function set(e){ const a=ang(e); w.firstElementChild.style.transform='rotate('+a+'deg)'; fxSet('sha',a); }
  w.addEventListener('mousedown', e=>{ e.preventDefault(); e.stopPropagation(); set(e); const mv=ev=>set(ev), up=()=>{ window.removeEventListener('mousemove',mv); window.removeEventListener('mouseup',up); }; window.addEventListener('mousemove',mv); window.addEventListener('mouseup',up); }); })();
el('fx_ol').onchange=e=>{ fxSet('ol',e.target.checked); fxSync(); }; el('fx_olc').oninput=e=>fxSet('olc',e.target.value); el('fx_olw').oninput=e=>fxSet('olw',+e.target.value);
el('fx_ola').onchange=e=>fxSet('ola',e.target.checked);
function openFxPop(p, btn){ selset.clear(); selset.add(p); pinHandles(); const fb=el('fxbody'); if(!fb) return; fb.classList.add('open');   // FX popover for one layer
  const r=btn.getBoundingClientRect(); let left=r.left-296; if(left<8) left=r.right+10; fb.style.left=Math.max(8,left)+'px';
  let top=r.top-6; if(top+fb.offsetHeight>window.innerHeight-8) top=window.innerHeight-8-fb.offsetHeight; fb.style.top=Math.max(8,top)+'px'; fxSync(); }
function closeFxPop(){ const fb=el('fxbody'); if(fb) fb.classList.remove('open'); }
el('fxpopx').onclick=()=>closeFxPop();
document.addEventListener('mousedown', e=>{ const fb=el('fxbody'); if(fb&&fb.classList.contains('open') && !e.target.closest('#fxbody') && !e.target.closest('.lfx')) closeFxPop(); });
el('cover_filter').onchange=e=>{ P.cover_filter=e.target.value; change(); };
document.querySelectorAll('#text_align button').forEach(b=>b.onclick=()=>{ P.text_align=b.dataset.v; document.querySelectorAll('#text_align button').forEach(x=>x.classList.toggle('on',x===b)); change(); });
document.querySelectorAll('#time_align button').forEach(b=>b.onclick=()=>{ P.time_align=b.dataset.v; document.querySelectorAll('#time_align button').forEach(x=>x.classList.toggle('on',x===b)); change(); pinHandles(); });
document.querySelectorAll('#vol_style button').forEach(b=>b.onclick=()=>{ P.vol_style=b.dataset.v; const L0=L(); if(L0.vol){ L0.vol.h=Math.round(L0.vol.w*(b.dataset.v==='bar'?0.469:0.755)); } document.querySelectorAll('#vol_style button').forEach(x=>x.classList.toggle('on',x===b)); change(); });
el('bars_attach').onchange=e=>{ P.bars_attach=e.target.checked; change(); };
el('bars_n').oninput=e=>{ P.bars_n=+e.target.value; sliderFill(e.target); change(); };
el('bars_gap').oninput=e=>{ P.bars_gap=+e.target.value/100; sliderFill(e.target); change(); };
el('bars_round').onchange=e=>{ P.bars_round=e.target.checked; change(); };
el('bars_glow').onchange=e=>{ P.bars_glow=e.target.checked; change(); };
el('bars_reactive').onchange=e=>{ P.bars_reactive=e.target.checked; change(); };
function editVisBBox(){ let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9,any=false;   // box of the visible layers = what OBS auto-crops to
  for(const p of ['bg','cover','text','bars','prog','vol','time','video']){ if(!visible(p)||(P.clip&&P.clip[p])) continue; const b=boxOf(p);   // clipped layers masked within their base -> excluded from the OBS size
    mnx=Math.min(mnx,b.x); mny=Math.min(mny,b.y); mxx=Math.max(mxx,b.x+b.w); mxy=Math.max(mxy,b.y+b.h); any=true; }
  return any?{x:mnx,y:mny,w:mxx-mnx,h:mxy-mny}:null; }
function updCanvDim(){ const bb=editVisBBox(), e=el('canvdim'); if(!e) return;
  e.textContent = bb ? (Math.round(bb.w)+' × '+Math.round(bb.h)+' px  ·  or any size') : '—'; }   // overlay scales to fill whatever OBS size you set
(function(){ const u=el('obsurl'); if(!u) return; u.value=location.origin+'/overlay';   // copyable OBS browser-source URL
  const b=el('copyurl'); if(b) b.onclick=()=>{ const t=u.value;
    try{ navigator.clipboard.writeText(t); }catch(e){ u.select(); try{document.execCommand('copy');}catch(_){} }
    const o=b.textContent; b.textContent='Copied!'; setTimeout(()=>{ b.textContent=o; },1200); }; })();
(function(){ const b=el('copysize'); if(!b) return; b.onclick=()=>{ const bb=editVisBBox(); if(!bb) return; const t=Math.round(bb.w)+'x'+Math.round(bb.h);
  try{ navigator.clipboard.writeText(t); }catch(e){} const o=b.textContent; b.textContent='Copied!'; setTimeout(()=>{ b.textContent=o; },1200); }; })();   // copy the content size for the OBS source
function refreshSel(){ document.querySelectorAll('.lrow').forEach(r=>r.classList.toggle('sel', selset.has(r.dataset.part)));
  const one=selset.size===1?[...selset][0]:null, w=el('opwrap');
  if(w){ w.classList.toggle('dim', !one);   // keep the row's space (no layout jump); just dim when nothing is selected
    if(one){ const raw=one==='bg'?(P.bg_opacity==null?0.85:P.bg_opacity):((P.op&&P.op[one]!=null)?P.op[one]:1);
      const v=Math.round(raw*100); el('oprange').value=v; el('opval').textContent=v+'%';
      const f=(P.fx&&P.fx[one])||{}; el('fx_sh').checked=!!f.sh; el('fx_shc').value=f.shc||'#000000'; el('fx_shb').value=f.shb==null?6:f.shb;
      el('fx_sho').value=f.sho==null?55:f.sho; el('fx_sha').firstElementChild.style.transform='rotate('+(f.sha==null?90:f.sha)+'deg)'; el('fx_shd').value=f.shd==null?4:f.shd;
      fxSync();
      el('fx_ol').checked=!!f.ol; el('fx_ola').checked=!!f.ola; el('fx_olc').value=f.olc||'#000000'; el('fx_olw').value=f.olw==null?2:f.olw; } }
  document.querySelectorAll('[data-for]').forEach(elm=>{ elm.style.display=(one&&elm.dataset.for.split(' ').includes(one))?'':'none'; });   // contextual: only the selected layer's controls
  document.querySelectorAll('.gradfld').forEach(e=>{ e.style.display=(P.bg_grad_user&&one==='bg')?'':'none'; });   // gradient colour-2 + angle: only in gradient mode
  const hint=el('prophint'); if(hint) hint.style.display=one?'none':''; }
el('oprange').oninput=e=>{ const one=selset.size===1?[...selset][0]:null; if(one==null) return;
  beginUndo(); const v=+e.target.value/100;
  if(one==='bg') P.bg_opacity=v;                 // bg: drive the card fill alpha (default see-through, 100% = solid)
  else { if(!P.op)P.op={}; P.op[one]=v; }
  el('opval').textContent=e.target.value+'%'; scheduleCommit(); applyCanvas(); save(); };
// drag to move + corner handles to resize (screen delta / ZOOM = canvas delta).
// Hold Shift while moving to snap edges/centers to other elements + canvas (with guides).
let drag=null, selset=new Set(), marq=null;
const SNAP=6, DIRS=['nw','n','ne','e','se','s','sw','w'], RESIZE=[{part:'bg',elid:'bg',dirs:DIRS},{part:'cover',elid:'art',dirs:DIRS},{part:'text',elid:'meta',dirs:DIRS},{part:'bars',elid:'bars',dirs:DIRS},{part:'prog',elid:'prog',dirs:DIRS},{part:'vol',elid:'vol',dirs:DIRS},{part:'time',elid:'time',dirs:DIRS},{part:'video',elid:'vid',dirs:DIRS}], handles=[], outlines={}, radhs=[], ghandles=[];
function buildOutlines(){ ['bg','cover','text','bars','prog','vol','time','video'].forEach(p=>{ const o=document.createElement('div');
  o.className='selbox'; el('stage').appendChild(o); outlines[p]=o; }); }
function partEl(p){ return p==='cover'?'art':p==='text'?'meta':p==='prog'?'prog':p==='bars'?'bars':p==='vol'?'vol':p==='time'?'time':p==='video'?'vid':'bg'; }
function partOf(id){ return id==='art'?'cover':id==='meta'?'text':id==='prog'?'prog':id==='bars'?'bars':id==='vol'?'vol':id==='time'?'time':id==='vid'?'video':'bg'; }
function visible(p){ return p==='bg'?P.bg_enabled!==false : p==='cover'?P.show_art!==false : p==='prog'?P.show_prog!==false : p==='bars'?P.show_bars!==false : p==='vol'?P.show_vol===true : p==='time'?P.show_time===true : p==='video'?P.show_video===true : true; }
function locked(p){ return !!(P.lock && P.lock[p]); }
function intersects(a,b){ return a.left<b.right && a.right>b.left && a.top<b.bottom && a.bottom>b.top; }
function boxFromO(it){ const b=it.o; if(it.p==='cover') return {x:b.x,y:b.y,w:b.size,h:b.size};
  if(it.p==='prog') return {x:b.x,y:b.y,w:b.w,h:b.h||4};
  if(it.p==='text'){ const r=el('meta').getBoundingClientRect(); return {x:b.x,y:b.y,w:r.width/ZOOM,h:r.height/ZOOM}; }
  return {x:b.x,y:b.y,w:b.w,h:b.h}; }
function cursorFor(d){ if(d==='n'||d==='s') return 'ns-resize'; if(d==='e'||d==='w') return 'ew-resize';
  return (d==='nw'||d==='se')?'nwse-resize':'nesw-resize'; }
function buildHandles(){ RESIZE.forEach(r=>r.dirs.forEach(c=>{ const h=document.createElement('div');
  h.className='hand'; h.style.cursor=cursorFor(c);
  h.addEventListener('mousedown', e=>{ e.preventDefault(); e.stopPropagation();
    beginUndo(); drag={mode:'size', part:r.part, dir:c, sx:e.clientX, sy:e.clientY, o:JSON.parse(JSON.stringify(L()[r.part]))}; });
  el('stage').appendChild(h); handles.push({node:h,part:r.part,corner:c,elid:r.elid}); }));
  ['nw','ne','sw','se'].forEach(cr=>{ const rh=document.createElement('div'); rh.className='radh'; rh.dataset.corner=cr;
    rh.addEventListener('mousedown', e=>{ e.preventDefault(); e.stopPropagation(); const one=selset.size===1?[...selset][0]:null;
      if(one!=='bg'&&one!=='cover'&&one!=='video') return; beginUndo();
      drag={mode:'radius', part:one, corner:cr, sx:e.clientX, sy:e.clientY, o:(one==='bg'?(P.bg_radius==null?13:P.bg_radius):one==='video'?(P.video_radius||0):coverRad())}; });
    el('stage').appendChild(rh); radhs.push(rh); });
  ['nw','ne','sw','se'].forEach(cr=>{ const g=document.createElement('div'); g.className='hand ghand'; g.style.cursor=cursorFor(cr);   // group-resize corner handles (multi-select)
    g.addEventListener('mousedown', e=>startGroupResize(cr,e)); el('stage').appendChild(g); ghandles.push({node:g,corner:cr}); }); }
function boxOf(part){ const lo=L();
  if(part==='video'){ const v=lo.video||{x:0,y:0,w:252,h:84}; return {x:v.x,y:v.y,w:v.w,h:v.h}; }
  if(part==='bg') return {x:lo.bg.x,y:lo.bg.y,w:lo.bg.w,h:lo.bg.h};
  if(part==='cover') return {x:lo.cover.x,y:lo.cover.y,w:lo.cover.size,h:lo.cover.size};
  if(part==='prog') return {x:lo.prog.x,y:lo.prog.y,w:lo.prog.w,h:lo.prog.h||4};
  if(part==='bars') return {x:lo.bars.x,y:lo.bars.y,w:lo.bars.w,h:lo.bars.h};
  if(part==='vol') return {x:lo.vol.x,y:lo.vol.y,w:lo.vol.w,h:lo.vol.h};
  if(part==='time'){ const r=el('time').getBoundingClientRect(), w=r.width/ZOOM, off=P.time_align==='center'?w/2:P.time_align==='right'?w:0; return {x:lo.time.x-off,y:lo.time.y,w:w,h:r.height/ZOOM}; }
  const r=el('meta').getBoundingClientRect(); return {x:lo.text.x,y:lo.text.y,w:lo.text.w!=null?lo.text.w:r.width/ZOOM,h:r.height/ZOOM}; }
function scalePart(p,o,s,ax,ay){ const lo=L(), nx=Math.round(ax+(o.x-ax)*s), ny=Math.round(ay+(o.y-ay)*s);   // scale a layer around the group anchor (ax,ay)
  if(p==='cover'){ lo.cover.x=nx; lo.cover.y=ny; lo.cover.size=Math.max(16,Math.round(o.size*s)); }
  else if(p==='text'){ lo.text.x=nx; lo.text.y=ny; lo.text.scale=Math.max(0.3,Math.min(5,Math.round((o.scale||1)*s*100)/100)); }
  else if(p==='prog'){ lo.prog.x=nx; lo.prog.y=ny; lo.prog.w=Math.max(24,Math.round(o.w*s)); }
  else { lo[p].x=nx; lo[p].y=ny; lo[p].w=Math.max(12,Math.round(o.w*s)); lo[p].h=Math.max(8,Math.round(o.h*s)); } }
function startGroupResize(dir, e){ e.preventDefault(); e.stopPropagation();
  const gm=[...selset].filter(p=>visible(p)&&!locked(p)); if(gm.length<2) return;
  let gx=1e9,gy=1e9,gx2=-1e9,gy2=-1e9; gm.forEach(p=>{ const b=boxOf(p); gx=Math.min(gx,b.x); gy=Math.min(gy,b.y); gx2=Math.max(gx2,b.x+b.w); gy2=Math.max(gy2,b.y+b.h); });
  beginUndo(); drag={mode:'gsize', dir, sx:e.clientX, sy:e.clientY, gw:Math.max(1,gx2-gx), gh:Math.max(1,gy2-gy),
    ax:dir.includes('w')?gx2:gx, ay:dir.includes('n')?gy2:gy, items:gm.map(p=>({p, o:JSON.parse(JSON.stringify(L()[p]))}))}; }
function coverRad(){ return P.cover_radius!=null?P.cover_radius:(P.cover_shape==='circle'?100:P.cover_shape==='sharp'?0:20); }
function artFilter(p){ return p.cover_filter==='grey'?'grayscale(1)':p.cover_filter==='sepia'?'grayscale(1) sepia(.7)':'none'; }
function fxFilter(pr, part){ const f=(pr.fx&&pr.fx[part])||{}; const s=[];   // per-layer FX as a composable CSS filter (outline = ring of 0-blur drop-shadows -> works on text/canvas/img)
  if(f.ol && (f.olw||0)>0 && part!=='cover' && part!=='bg' && part!=='prog' && part!=='video'){ const c=olColor(f), w=f.olw, N=8; for(let k=0;k<N;k++){ const a=k*6.2831853/N; s.push('drop-shadow('+(Math.cos(a)*w).toFixed(2)+'px '+(Math.sin(a)*w).toFixed(2)+'px 0 '+c+')'); } }   // content layers (text/bars/vol/time): light 8-dir ring. shaped layers use box-shadow (fxBoxShadow) -> cheap + perfect circle, no 24-filter crash
  if(f.sh){ const a=(f.sha==null?90:f.sha)*Math.PI/180, d=(f.shd==null?4:f.shd), op=(f.sho==null?55:f.sho)/100; s.push('drop-shadow('+(Math.cos(a)*d).toFixed(1)+'px '+(Math.sin(a)*d).toFixed(1)+'px '+(f.shb==null?6:f.shb)+'px '+hexA(f.shc||'#000000',op)+')'); }
  return s.join(' '); }
function fxBoxShadow(pr, part){ const f=(pr.fx&&pr.fx[part])||{};   // outline for SHAPED layers: one spread shadow follows border-radius (perfect circle, GPU-cheap)
  if(f.ol && (f.olw||0)>0 && (part==='cover'||part==='bg'||part==='prog'||part==='video')) return '0 0 0 '+f.olw+'px '+olColor(f);
  return ''; }
function olColor(f){ return (f.ola && _autoRGB) ? 'rgb('+_autoRGB[0]+','+_autoRGB[1]+','+_autoRGB[2]+')' : (f.olc||'#000000'); }   // outline colour: auto-sampled from cover when f.ola, else manual
function anyFxAuto(p){ if(!p||!p.fx) return false; for(const k in p.fx){ if(p.fx[k]&&p.fx[k].ola) return true; } return false; }
function applyVideo(pr, vd){ if(!vd) return;   // video layer: editor test URL (pr.video_url) or live np video; loop/mute/cover
  const vsrc=((pr.video_url||'').trim())||_npVideo||'', on=pr.show_video===true, fit=pr.video_fit||'cover', rad=(pr.video_radius||0)+'px';
  if(!on){ vd.style.display='none'; if(vd._src){ vd._src=''; vd.removeAttribute('src'); vd.load(); } vd.style.backgroundImage='none'; return; }
  vd.style.display='block'; vd.style.objectFit=fit; vd.style.borderRadius=rad;
  if(vsrc){ if(vd._src!==vsrc){ vd._src=vsrc; vd.src=vsrc; vd.play().catch(()=>{}); } vd.style.backgroundImage='none'; }
  else { if(vd._src){ vd._src=''; vd.removeAttribute('src'); vd.load(); }   // no Canvas -> album-art placeholder in the same box
    const a=document.getElementById('art'), u=a&&a.getAttribute('src'); vd.style.backgroundImage=u?('url("'+u+'")'):'none';
    vd.style.backgroundSize=(fit==='contain'?'contain':fit==='fill'?'100% 100%':'cover'); vd.style.backgroundPosition='center'; vd.style.backgroundRepeat='no-repeat'; } }
function partRadius(part,b){ if(part==='bg') return P.bg_radius==null?13:P.bg_radius;
  if(part==='video') return P.video_radius||0;
  if(part==='cover') return coverRad()/100*Math.min(b.w,b.h)/2; return 0; }
function rrPath(x,y,w,h,r){ const x2=x+w, y2=y+h;   // SVG path of a rounded rect (accurate clip, any size/radius)
  if(r<=0.5) return "path('M"+x+" "+y+" H"+x2+" V"+y2+" H"+x+" Z')";
  return "path('M"+(x+r)+" "+y+" H"+(x2-r)+" A"+r+" "+r+" 0 0 1 "+x2+" "+(y+r)+" V"+(y2-r)+" A"+r+" "+r+" 0 0 1 "+(x2-r)+" "+y2+" H"+(x+r)+" A"+r+" "+r+" 0 0 1 "+x+" "+(y2-r)+" V"+(y+r)+" A"+r+" "+r+" 0 0 1 "+(x+r)+" "+y+" Z')"; }
function clipBase(o,i){ for(let j=i-1;j>=0;j--){ if(!(P.clip&&P.clip[o[j]])) return j; } return -1; }   // first NON-clipped layer below -> many layers can share one base (e.g. all clip to bg)
function applyClip(part){ const e=el(partEl(part)); if(!e) return; const o=order(), i=o.indexOf(part);
  if(!(P.clip&&P.clip[part])){ e.style.clipPath='none'; return; }
  const bi=clipBase(o,i); if(bi<0){ e.style.clipPath='none'; return; }
  const A=boxOf(part), B=boxOf(o[bi]); let R=partRadius(o[bi],B); R=Math.max(0,Math.min(R,Math.min(B.w,B.h)/2));
  const sc=(part==='text')?(L().text.scale||1):1;   // #meta is transform:scaled -> clip-path lives in its pre-scale local coords
  e.style.clipPath=rrPath((B.x-A.x)/sc, (B.y-A.y)/sc, B.w/sc, B.h/sc, R/sc); }
function lineset(parts,axis){ const lo=L(); const out=axis==='x'?[0,lo.cw,lo.cw/2]:[0,lo.ch,lo.ch/2];
  for(const p of ['bg','cover','text','bars','prog','vol','time','video']){ if(parts.indexOf(p)>=0) continue; const b=boxOf(p);
    if(axis==='x') out.push(b.x,b.x+b.w,b.x+b.w/2); else out.push(b.y,b.y+b.h,b.y+b.h/2); }
  out.sort((a,b)=>a-b); const dd=[];   // dedupe near-equal lines (e.g. bg centre == canvas centre)
  for(const v of out){ if(!dd.length||Math.abs(v-dd[dd.length-1])>1.5) dd.push(v); } return dd; }
function canvasLines(axis){ const lo=L(); return axis==='x'?[0,lo.cw,lo.cw/2]:[0,lo.ch,lo.ch/2]; }
function nearest(pts,lns,th){ th=th||SNAP; let best=null; for(const pt of pts) for(const ln of lns){ const d=ln-pt;
  if(Math.abs(d)<=th && (!best||Math.abs(d)<Math.abs(best.d))) best={d,ln}; } return best; }
function guide(id,val,color){ const g=el(id), lo=L(); if(val==null){ g.style.display='none'; return; }
  g.style.display='block'; g.style.background=color||'#ffffff';   // white = normal snap, orange = canvas centre, blue = shift axis-lock
  if(id==='gx'){ g.style.left=val+'px'; g.style.height=lo.ch+'px'; } else { g.style.top=val+'px'; g.style.width=lo.cw+'px'; } }
['bg','art','meta','prog','bars','vol','time','vid'].forEach(id=>el(id).addEventListener('mousedown', e=>{
  if(locked(partOf(id))) return;                     // locked layer: no canvas drag/select
  if(id==='bars' && P.bars_attach!==false) return;   // attached bars rides the text -> let it bubble to #meta
  e.preventDefault(); e.stopPropagation(); const p=partOf(id); beginUndo();
  if(!e.shiftKey && !selset.has(p)){ selset.clear(); selset.add(p); }   // plain click on a new element selects it
  pinHandles();
  drag={mode:'move', sx:e.clientX, sy:e.clientY, moved:false, shift:e.shiftKey, clickPart:p,
        items:[...selset].map(q=>({p:q,o:JSON.parse(JSON.stringify(L()[q]))}))};
}));
el('stage').addEventListener('mousedown', e=>{ const s=el('stage').getBoundingClientRect();   // empty -> marquee
  marq={sx:e.clientX,sy:e.clientY,shift:e.shiftKey,moved:false};
  const m=el('marq'); m.style.left=(e.clientX-s.left)+'px'; m.style.top=(e.clientY-s.top)+'px';
  m.style.width='0px'; m.style.height='0px'; });
document.addEventListener('mousedown', e=>{   // clicking the black (outside stage + panel) deselects
  if(!e.target.closest('#stage') && !e.target.closest('#dock')){ selset.clear(); pinHandles(); } });
window.addEventListener('mousemove', e=>{
  if(marq){ const s=el('stage').getBoundingClientRect(), x0=marq.sx-s.left, y0=marq.sy-s.top, x1=e.clientX-s.left, y1=e.clientY-s.top;
    if(Math.abs(x1-x0)+Math.abs(y1-y0)>3) marq.moved=true; const m=el('marq');
    m.style.display=marq.moved?'block':'none'; m.style.left=Math.min(x0,x1)+'px'; m.style.top=Math.min(y0,y1)+'px';
    m.style.width=Math.abs(x1-x0)+'px'; m.style.height=Math.abs(y1-y0)+'px';
    const mr=m.getBoundingClientRect(), prev=new Set(marq.shift?[...selset]:[]);   // live preview of what the box covers
    if(marq.moved) ['bg','cover','text','bars','prog','vol','time','video'].forEach(p=>{ if(visible(p)&&!locked(p)&&intersects(el(partEl(p)).getBoundingClientRect(), mr)) prev.add(p); });
    drawOutlines(prev); return; }
  if(!drag) return;
  if(drag.mode==='pan'){ panX=drag.px+(e.clientX-drag.sx); panY=drag.py+(e.clientY-drag.sy);
    updateScroll(); pinHandles(); return; }
  const dx=(e.clientX-drag.sx)/ZOOM, dy=(e.clientY-drag.sy)/ZOOM, lo=L();
  if(Math.abs(e.clientX-drag.sx)+Math.abs(e.clientY-drag.sy)>2) drag.moved=true;
  if(drag.mode==='radius'){ const c=drag.corner||'nw', sgx=(c==='nw'||c==='sw')?1:-1, sgy=(c==='nw'||c==='ne')?1:-1, dr=(sgx*dx+sgy*dy)/2;   // drag corner inward -> rounder
    if(drag.part==='bg') P.bg_radius=Math.max(0,Math.min(200,Math.round(drag.o+dr)));
    else if(drag.part==='video') P.video_radius=Math.max(0,Math.min(400,Math.round(drag.o+dr)));
    else { const cb=boxOf('cover'), half=Math.min(cb.w,cb.h)/2||1; P.cover_radius=Math.max(0,Math.min(100,Math.round(drag.o+dr/half*100))); }
    syncControls(); applyCanvas(); save(); return; }
  if(drag.mode==='gsize'){ const d=drag.dir, sgx=d.includes('w')?-1:1, sgy=d.includes('n')?-1:1;   // group resize: one uniform scale around the opposite-corner anchor
    const sx=(drag.gw+sgx*dx)/drag.gw, sy=(drag.gh+sgy*dy)/drag.gh, s=Math.max(0.15,(Math.abs(sx-1)>=Math.abs(sy-1))?sx:sy);
    drag.items.forEach(it=>scalePart(it.p, it.o, s, drag.ax, drag.ay)); applyCanvas(); save(); return; }
  if(drag.mode==='move'){
    if(drag.shift && drag.clickPart && !selset.has(drag.clickPart)){ selset.add(drag.clickPart); pinHandles();
      drag.items=[...selset].map(q=>({p:q,o:JSON.parse(JSON.stringify(L()[q]))})); }
    let ox=Math.round(dx), oy=Math.round(dy); const parts=drag.items.map(it=>it.p);
    let mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
    drag.items.forEach(it=>{ const b=boxFromO(it); mnx=Math.min(mnx,b.x); mny=Math.min(mny,b.y); mxx=Math.max(mxx,b.x+b.w); mxy=Math.max(mxy,b.y+b.h); });
    if(e.shiftKey){   // Shift = lock to one axis (Photoshop) + show that axis line through the centre, no snapping
      if(Math.abs(e.clientX-drag.sx) >= Math.abs(e.clientY-drag.sy)){ oy=0; guide('gy',(mny+mxy)/2,'#7AA2FF'); guide('gx',null); }
      else { ox=0; guide('gx',(mnx+mxx)/2,'#7AA2FF'); guide('gy',null); }
    } else {   // snapping is always on but TIGHT: canvas edges/centre first (easy to hit), then only very-close element edges/centres
      const multi=parts.length>1;
      const ptsx=[mnx+ox,(mnx+mxx)/2+ox,mxx+ox], ptsy=[mny+oy,(mny+mxy)/2+oy,mxy+oy];
      const sx=nearest(ptsx,canvasLines('x'),7) || (multi?null:nearest(ptsx,lineset(parts,'x'),4));
      const sy=nearest(ptsy,canvasLines('y'),7) || (multi?null:nearest(ptsy,lineset(parts,'y'),4));
      if(sx){ ox+=Math.round(sx.d); guide('gx',sx.ln, sx.ln===L().cw/2?'#FF7A1A':'#ffffff');} else guide('gx',null);
      if(sy){ oy+=Math.round(sy.d); guide('gy',sy.ln, sy.ln===L().ch/2?'#FF7A1A':'#ffffff');} else guide('gy',null);
    }
    const lo2=L(), M=-Math.max(lo2.cw,lo2.ch);   // allow dragging well off-canvas (no hard wall); still bounded so nothing is lost forever
    ox=Math.max(M-mxx, Math.min(lo2.cw-M-mnx, ox));
    oy=Math.max(M-mxy, Math.min(lo2.ch-M-mny, oy));
    drag.items.forEach(it=>{ lo[it.p].x=it.o.x+ox; lo[it.p].y=it.o.y+oy; });
  } else {
    const o=drag.o, d=drag.dir, hasW=d.includes('w'), hasE=d.includes('e'), hasN=d.includes('n'), hasS=d.includes('s');
    const alt=e.altKey, snap=!e.shiftKey && !alt;   // snap on by default (tight); Shift = free (no snap); Alt = resize around centre
    if(drag.part==='cover'){
      const ax=hasW?o.x+o.size:o.x, ay=hasN?o.y+o.size:o.y;   // fixed anchor (non-alt)
      let sd=0,n=0; if(hasE){sd+=dx;n++;} if(hasW){sd-=dx;n++;} if(hasS){sd+=dy;n++;} if(hasN){sd-=dy;n++;}
      let ns=Math.max(24, o.size+(alt?2:1)*(n?sd/n:0));
      if(snap){ guide('gx',null); guide('gy',null);
        const mvx=hasW?ax-ns:hasE?ax+ns:null, mvy=hasN?ay-ns:hasS?ay+ns:null; let best=null;
        if(mvx!=null){ const s=nearest([mvx], lineset(['cover'],'x')); if(s) best={d:Math.abs(s.d),ln:s.ln,ax:1}; }
        if(mvy!=null){ const s=nearest([mvy], lineset(['cover'],'y')); if(s&&(!best||Math.abs(s.d)<best.d)) best={d:Math.abs(s.d),ln:s.ln,ax:0}; }
        if(best){ ns=Math.max(24, Math.abs(best.ax?(best.ln-ax):(best.ln-ay))); best.ax?guide('gx',best.ln):guide('gy',best.ln); }
      } else { guide('gx',null); guide('gy',null); }
      ns=Math.round(ns);
      if(alt){ const cx=o.x+o.size/2, cy=o.y+o.size/2; lo.cover.x=Math.round(cx-ns/2); lo.cover.y=Math.round(cy-ns/2); }
      else { lo.cover.x=hasW?ax-ns:o.x; lo.cover.y=hasN?ay-ns:o.y; }
      lo.cover.size=ns;
    } else if(drag.part==='text'){
      if((hasE||hasW)&&!hasN&&!hasS){   // horizontal handle = set text-box width (the marquee clip); no scaling
        const base=(o.w!=null)?o.w:Math.max(40,(lo.bg.x+lo.bg.w)-o.x-14);
        const nw=Math.max(40, Math.round(base+(hasE?dx:-dx)));
        if(hasW) lo.text.x=Math.round(o.x-(nw-base));
        lo.text.w=nw;
      } else {   // corner / vertical handle = scale the text block, anchored at the opposite corner/edge
        const w0=el('meta').offsetWidth||120, h0=el('meta').offsetHeight||40, W0=w0*(o.scale||1), H0=h0*(o.scale||1);
        let sd=0,n=0; if(hasE){sd+=dx;n++;} if(hasW){sd-=dx;n++;} if(hasS){sd+=dy;n++;} if(hasN){sd-=dy;n++;}
        const nscale=Math.max(0.4, Math.min(4, (o.scale||1)+(n?sd/n:0)/w0));
        if(alt){ const cx=o.x+W0/2, cy=o.y+H0/2; lo.text.x=Math.round(cx-w0*nscale/2); lo.text.y=Math.round(cy-h0*nscale/2); }
        else { lo.text.x=hasW?Math.round((o.x+W0)-w0*nscale):o.x; lo.text.y=hasN?Math.round((o.y+H0)-h0*nscale):o.y; }
        lo.text.scale=Math.round(nscale*100)/100;
      }
    } else if(drag.part==='vol' && P.vol_style!=='bar'){   // arcs meter: aspect-locked resize (no distort). bar style -> falls through to free w/h so you can widen the bar
      const ar=o.h/o.w, axv=hasW?o.x+o.w:o.x, ayv=hasN?o.y+o.h:o.y;
      let sd=0,n=0; if(hasE){sd+=dx;n++;} if(hasW){sd-=dx;n++;} if(hasS){sd+=dy/ar;n++;} if(hasN){sd-=dy/ar;n++;}
      const nw=Math.max(20, Math.round(o.w+(alt?2:1)*(n?sd/n:0))), nh=Math.round(nw*ar);
      if(alt){ const cx=o.x+o.w/2, cy=o.y+o.h/2; lo.vol.x=Math.round(cx-nw/2); lo.vol.y=Math.round(cy-nh/2); }
      else { lo.vol.x=hasW?axv-nw:o.x; lo.vol.y=hasN?ayv-nh:o.y; }
      lo.vol.w=nw; lo.vol.h=nh;
    } else if(drag.part==='time'){   // counter hugs its text, so w/h handles have nothing to grab -> any handle scales the font (lo.time.h), anchored top-left
      const h0=o.h||16, base=Math.max(40, o.w||96);
      let sd=0,n=0; if(hasE){sd+=dx;n++;} if(hasW){sd-=dx;n++;} if(hasS){sd+=dy;n++;} if(hasN){sd-=dy;n++;}
      lo.time.h=Math.max(8, Math.min(160, Math.round(h0*(1+(n?sd/n:0)/base))));
    } else {   // bg / bars / vol-bar / prog: free w/h (Alt = from centre; Alt+Shift = keep aspect)
      const mnW=(drag.part==='bars'||drag.part==='vol'||drag.part==='prog')?16:60, mnH=drag.part==='prog'?3:(drag.part==='bars'||drag.part==='vol')?8:30;
      if(alt && e.shiftKey){   // keep the original aspect ratio, scaled from the centre
        const ar=o.h/o.w||1; let sd=0,n=0; if(hasE){sd+=dx;n++;} if(hasW){sd-=dx;n++;} if(hasS){sd+=dy/ar;n++;} if(hasN){sd-=dy/ar;n++;}
        const nw=Math.max(mnW, Math.round(o.w+2*(n?sd/n:0))), nh=Math.max(mnH, Math.round(nw*ar));
        lo[drag.part]={x:Math.round(o.x+o.w/2-nw/2), y:Math.round(o.y+o.h/2-nh/2), w:nw, h:nh}; guide('gx',null); guide('gy',null);
      } else {
        let w=o.w, h=o.h, xx=o.x, yy=o.y; const cx=o.x+o.w/2, cy=o.y+o.h/2;
        if(hasE||hasW){ const dw=hasE?dx:-dx;
          if(alt){ w=Math.max(mnW,o.w+2*dw); xx=cx-w/2; }
          else if(hasE){ w=Math.max(mnW,o.w+dw); xx=o.x; } else { w=Math.max(mnW,o.w+dw); xx=(o.x+o.w)-w; } }
        if(hasS||hasN){ const dh=hasS?dy:-dy;
          if(alt){ h=Math.max(mnH,o.h+2*dh); yy=cy-h/2; }
          else if(hasS){ h=Math.max(mnH,o.h+dh); yy=o.y; } else { h=Math.max(mnH,o.h+dh); yy=(o.y+o.h)-h; } }
        if(snap){ guide('gx',null); guide('gy',null);
          if(hasE){ const s=nearest([xx+w], lineset([drag.part],'x')); if(s){ w=Math.max(mnW,w+s.d); guide('gx',s.ln);} }
          else if(hasW){ const s=nearest([xx], lineset([drag.part],'x')); if(s){ xx+=s.d; w=Math.max(mnW,w-s.d); guide('gx',s.ln);} }
          if(hasS){ const s=nearest([yy+h], lineset([drag.part],'y')); if(s){ h=Math.max(mnH,h+s.d); guide('gy',s.ln);} }
          else if(hasN){ const s=nearest([yy], lineset([drag.part],'y')); if(s){ yy+=s.d; h=Math.max(mnH,h-s.d); guide('gy',s.ln);} }
        } else { guide('gx',null); guide('gy',null); }
        lo[drag.part]={x:Math.round(xx),y:Math.round(yy),w:Math.round(w),h:Math.round(h)};
      }
    }
  }
  applyCanvas(); save(); });
window.addEventListener('mouseup', e=>{
  if(marq){ const m=el('marq');
    if(marq.moved){ const mr=m.getBoundingClientRect(); if(!marq.shift) selset.clear();
      ['bg','cover','text','bars','prog','vol','time','video'].forEach(p=>{ if(visible(p) && !locked(p) && intersects(el(partEl(p)).getBoundingClientRect(), mr)) selset.add(p); });
    } else if(!marq.shift){ selset.clear(); }
    m.style.display='none'; marq=null; guide('gx',null); guide('gy',null); pinHandles(); save(); return; }
  if(drag){ if(drag.mode==='move' && !drag.moved){   // click (no move) = (de)select
      if(drag.shift){ selset.has(drag.clickPart)?selset.delete(drag.clickPart):selset.add(drag.clickPart); }
      else { selset.clear(); selset.add(drag.clickPart); } pinHandles(); }
    guide('gx',null); guide('gy',null); drag=null; commitUndo(); }
});
buildHandles(); buildOutlines(); dragScroll(el('hthumb'),'x'); dragScroll(el('vthumb'),'y'); window.addEventListener('resize', updateScroll);
el('stage').addEventListener('wheel', e=>{ e.preventDefault();   // scroll = pan (slow), Ctrl+scroll = horizontal, Alt+scroll = zoom
  const K=0.35;   // pan speed (raw wheel deltas are too fast)
  if(e.altKey){ const z0=ZOOM, sr=el('stage').getBoundingClientRect(); ZOOM=Math.max(0.5,Math.min(6, ZOOM*(e.deltaY<0?1.12:0.89)));   // zoom toward the cursor (Photoshop)
    const rx=e.clientX-(sr.left+sr.width/2), ry=e.clientY-(sr.top+sr.height/2), k=ZOOM/z0; panX=rx-(rx-panX)*k; panY=ry-(ry-panY)*k; }
  else if(e.ctrlKey||e.shiftKey){ panX -= e.deltaY*K; }
  else { panX -= e.deltaX*K; panY -= e.deltaY*K; }
  updateScroll(); pinHandles(); }, {passive:false});
el('stage').addEventListener('mousedown', e=>{ if(e.button!==1) return;   // middle-mouse drag = pan
  e.preventDefault(); e.stopPropagation(); drag={mode:'pan', sx:e.clientX, sy:e.clientY, px:panX, py:panY}; }, true);
window.addEventListener('keydown', e=>{ const t=(e.target&&e.target.tagName)||''; if(/INPUT|SELECT|TEXTAREA/.test(t)) return;
  if(e.key==='0'){ ZOOM=2; panX=0; panY=0; applyCanvas(); }   // 0 = reset zoom + pan
  else if(e.key==='Backspace'||e.key==='Delete'){ e.preventDefault(); const hit=[...selset].filter(p=>visible(p)); if(hit.length){ beginUndo(); hit.forEach(toggleLayer); commitUndo(); applyCanvas(); syncControls(); save(); buildLayers(); refreshSel(); } } });   // backspace/del = hide selected layer(s)
document.querySelectorAll('.dhead').forEach(h=>{ if(!h.querySelector('.chev')) return; h.addEventListener('click',()=>h.parentElement.classList.toggle('collapsed')); });   // only panels with a chevron collapse (Layers is static)
let lastVer=null;
const _PLAY_SVG='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"/></svg>';
const _PAUSE_SVG='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5" width="4" height="14" rx="1.3"/><rect x="13.5" y="5" width="4" height="14" rx="1.3"/></svg>';
let _lastPlay=null;
function setPlayIcon(on){ if(on===_lastPlay) return; _lastPlay=on; const b=el('t_play'); if(b) b.innerHTML=on?_PAUSE_SVG:_PLAY_SVG; }
function ctl(action){ fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})}).catch(()=>{}); }   // editor transport -> app's routed media actions
el('t_prev').onclick=()=>ctl('prev'); el('t_next').onclick=()=>ctl('next'); el('t_play').onclick=()=>ctl('playpause');
async function np(){ try{ const d=await(await fetch('/np',{cache:'no-store'})).json();
  { const play=!!d.playing; if(npPlaying!==play||d.ver!==_lastClkVer){ npPos=d.pos||0; npAt=performance.now(); _lastClkVer=d.ver; } npDur=d.dur||0; npPlaying=play; } _vol=(typeof d.volume==='number')?d.volume:_vol; setPlayIcon(npPlaying); _npVideo=d.video||'';   // free local clock + live Canvas/video URL
  if(d.ver!==lastVer){ lastVer=d.ver;
    el('title').textContent=d.title||'Title'; el('artistname').textContent=d.artist||'Artist';
    marquee('title'); marquee('artistname');
    const A=el('art');
    if(d.art){ const u='/art?v='+encodeURIComponent(d.ver); A.classList.remove('noart');
      A.onload=()=>{ _autoRGB=null; if(P.bg_auto||P.accent_auto||P.prog_auto||anyFxAuto(P)) _autoRGB=sampleArt(A); applyCanvas(); };   // fresh cover -> resample auto-colours + full re-render (no stale colours)
      A.src=u; el('coverbg').style.backgroundImage="url('"+u+"')"; }
    else { A.onload=null; A.classList.add('noart'); A.src=NOART; } }
  pinHandles(); }catch(e){} }
window.addEventListener('resize', pinHandles);
function flash(t){ const sv=el('saved'); if(!sv) return; sv.textContent=t||'saved'; sv.style.opacity='1'; clearTimeout(flash._t); flash._t=setTimeout(()=>sv.style.opacity='0',1200); }
async function presetsRefresh(active){ try{ const d=await(await fetch('/presets',{cache:'no-store'})).json(); const sel=el('preset_sel'); const cur=active||d.active||sel.value;
  sel.innerHTML=''; if(!d.names.length){ const o=document.createElement('option'); o.value=''; o.textContent='(no saved presets)'; sel.appendChild(o); }
  d.names.forEach(n=>{ const o=document.createElement('option'); o.value=n; o.textContent=n; sel.appendChild(o); }); if(cur&&d.names.includes(cur)) sel.value=cur; }catch(e){} }
async function presetSave(name){ try{ await fetch('/presets/save?name='+encodeURIComponent(name),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(P)}); await presetsRefresh(name); flash('saved'); }catch(e){} }
async function presetLoad(name){ if(!name) return; try{ const d=await(await fetch('/presets/load?name='+encodeURIComponent(name),{method:'POST'})).json(); if(d&&d.preset){ P=d.preset; L(); selset.clear(); syncControls(); applyCanvas(); buildLayers(); flash('loaded'); } }catch(e){} }
function presetsInit(){ el('preset_save').onclick=()=>{ let n=el('preset_sel').value; if(!n){ n=(prompt('Preset name:','My overlay')||'').trim(); if(!n) return; } presetSave(n); };
  el('preset_saveas').onclick=()=>{ const n=(prompt('Save as new preset:','')||'').trim(); if(n) presetSave(n); };
  el('preset_del').onclick=async()=>{ const n=el('preset_sel').value; if(!n) return; if(!confirm('Delete preset "'+n+'"?')) return; try{ await fetch('/presets/del?name='+encodeURIComponent(n),{method:'POST'}); await presetsRefresh(); }catch(e){} };
  el('preset_sel').onchange=()=>presetLoad(el('preset_sel').value); presetsRefresh(); }
(async()=>{ try{ P=await(await fetch('/preset',{cache:'no-store'})).json(); }catch(e){ P={}; }
  L(); syncControls(); applyCanvas(); buildLayers(); presetsInit(); np(); setInterval(np,1000);
  async function fetchBars(){ try{ const d=await(await fetch('/bars',{cache:'no-store'})).json(); _barLv=(Array.isArray(d.bars)&&d.bars.length>=3)?d.bars:null; }catch(e){} }
  fetchBars(); setInterval(fetchBars,40);   // live EQ levels in the editor preview too
  let _boot=null; setInterval(async()=>{ try{ const b=(await(await fetch('/boot',{cache:'no-store'})).json()).boot; if(_boot&&b!==_boot) location.reload(); _boot=b; }catch(e){} }, 1500);
  })();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return None

    def _send(self, code, ctype, body=b'', no_cache=False):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self._send_cors()
            if no_cache:
                self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if body:
                self.wfile.write(body)
        except Exception:
            pass

    def _allowed_origin(self):
        o = self.headers.get('Origin')
        if not o:
            return None
        if o == 'https://getsegue.app':
            return o
        if o in ('http://localhost', 'http://127.0.0.1') or o.startswith('http://localhost:') or o.startswith('http://127.0.0.1:'):
            return o
        return None

    def _send_cors(self):
        o = self._allowed_origin()
        if not o:
            return None
        self.send_header('Access-Control-Allow-Origin', o)
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self._send_cors()
            self.send_header('Content-Length', '0')
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            ui = getattr(self.server, 'ui', {}) or {}
            path = self.path.split('?', 1)[0]
            if path in ('/', '/overlay', '/index.html'):
                self._send(200, 'text/html; charset=utf-8', _OVERLAY_HTML.encode('utf-8'))
                return None
            if path == '/np':
                title = ui.get('np_title', '') or ''
                artist = ui.get('np_artist', '') or ''
                thumb = ui.get('np_thumb')
                ver = hash((title, artist, bytes(thumb) if thumb else b'')) & 4294967295
                body = json.dumps({'title': title, 'artist': artist, 'playing': bool(ui.get('np_playing', False)), 'app': ui.get('np_app', '') or '', 'pos': float(ui.get('np_pos', 0.0) or 0.0), 'dur': float(ui.get('np_dur', 0.0) or 0.0), 'art': bool(thumb), 'video': ui.get('np_video', '') or '', 'ver': str(ver), 'volume': float(ui.get('volume', 1.0) or 0.0), 'bars': getattr(self.server, 'bars', None).levels if getattr(self.server, 'bars', None) is not None else None}).encode('utf-8')
                self._send(200, 'application/json', body, no_cache=True)
                return None
            if path == '/bars':
                _b = getattr(self.server, 'bars', None)
                _payload = {'bars': _b.levels if _b is not None else None}
                if _b is not None:
                    _payload['excite'] = float(getattr(_b, 'excite', 0.0))
                    _lufs_s = getattr(_b, 'lufs_s', None)
                    if _lufs_s is not None:
                        _payload['lufs_s'] = float(_lufs_s)
                    _lufs_m = getattr(_b, 'lufs_m', None)
                    if _lufs_m is not None:
                        _payload['lufs_m'] = float(_lufs_m)
                self._send(200, 'application/json', json.dumps(_payload).encode('utf-8'), no_cache=True)
                return None
            if path == '/boot':
                self._send(200, 'application/json', json.dumps({'boot': BOOT_ID}).encode('utf-8'), no_cache=True)
                return None
            if path == '/art':
                data = ui.get('np_thumb')
                if not data:
                    self._send(204, 'image/png', b'')
                    return None
                self._send(200, 'image/png', bytes(data), no_cache=True)
                return None
            if path == '/preset':
                preset = getattr(self.server, 'preset', DEFAULT_PRESET)
                self._send(200, 'application/json', json.dumps(preset).encode('utf-8'), no_cache=True)
                return None
            if path == '/presets':
                pr = getattr(self.server, 'presets', {}) or {}
                self._send(200, 'application/json', json.dumps({'names': sorted(pr), 'active': getattr(self.server, 'preset_name', '')}).encode('utf-8'), no_cache=True)
                return None
            if path == '/editor':
                self._send(200, 'text/html; charset=utf-8', _EDITOR_HTML.encode('utf-8'))
                return None
            if path == '/logo':
                try:
                    with open(_LOGO_PATH, 'rb') as f:
                        self._send(200, 'image/png', f.read())
                    return None
                except Exception:
                    self._send(404, 'image/png', b'')
                    return None
            if path == '/font/inter.ttf':
                try:
                    with open(_INTER_PATH, 'rb') as f:
                        self._send(200, 'font/ttf', f.read())
                    return None
                except Exception:
                    self._send(404, 'font/ttf', b'')
                    return None
            if path == '/viz_layer.js':
                try:
                    with open(_VIZ_JS_PATH, 'rb') as f:
                        self._send(200, 'application/javascript', f.read())
                    return None
                except Exception:
                    self._send(404, 'application/javascript', b'')
                    return None
            if path in ('/kofi.png', '/discord.png'):
                try:
                    with open(_KOFI_PATH if path == '/kofi.png' else _DISCORD_PATH, 'rb') as f:
                        self._send(200, 'image/png', f.read())
                    return None
                except Exception:
                    self._send(404, 'image/png', b'')
                    return None
            self._send(404, 'text/plain', b'not found')
        except Exception:
            pass

    def _persist_presets(self):
        cb = getattr(self.server, 'on_persist', None)
        if cb:
            try:
                cb(dict(getattr(self.server, 'presets', {}) or {}), getattr(self.server, 'preset_name', ''))
            except Exception:
                pass

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
            except Exception:
                body = {}
            presets = getattr(self.server, 'presets', None)
            if path == '/preset':
                if isinstance(body, dict):
                    self.server.preset = dict(DEFAULT_PRESET, **body)
                    cb = getattr(self.server, 'on_save', None)
                    if cb:
                        cb(dict(self.server.preset))
                self._send(200, 'application/json', b'{"ok":true}', no_cache=True)
                return None
            if path == '/control':
                act = (body.get('action') if isinstance(body, dict) else '') or ''
                ui = getattr(self.server, 'ui', None)
                fn = ui.get('routed_' + act) if ui is not None and act in ('prev', 'playpause', 'next') else None
                if fn:
                    fn()
                self._send(200, 'application/json', b'{"ok":true}', no_cache=True)
                return None
            if path == '/presets/save' and presets is not None:
                if isinstance(body, dict):
                    name = (q.get('name') or '').strip() or 'Untitled'
                    presets[name] = dict(DEFAULT_PRESET, **body)
                    self.server.preset_name = name
                    self._persist_presets()
                    self._send(200, 'application/json', json.dumps({'ok': True, 'names': sorted(presets), 'active': name}).encode('utf-8'), no_cache=True)
                    return None
            if path == '/presets/load' and presets is not None:
                name = (q.get('name') or '').strip()
                if name in presets:
                    self.server.preset = dict(DEFAULT_PRESET, **presets[name])
                    self.server.preset_name = name
                    cb = getattr(self.server, 'on_save', None)
                    if cb:
                        cb(dict(self.server.preset))
                    self._persist_presets()
                    self._send(200, 'application/json', json.dumps({'ok': True, 'preset': self.server.preset, 'active': name}).encode('utf-8'), no_cache=True)
                    return None
                self._send(404, 'application/json', b'{"ok":false}')
                return None
            if path == '/presets/del' and presets is not None:
                name = (q.get('name') or '').strip()
                presets.pop(name, None)
                if getattr(self.server, 'preset_name', '') == name:
                    self.server.preset_name = ''
                self._persist_presets()
                self._send(200, 'application/json', json.dumps({'ok': True, 'names': sorted(presets)}).encode('utf-8'), no_cache=True)
                return None
            self._send(404, 'text/plain', b'not found')
        except Exception:
            pass


class StreamOverlayServer:
    """Serves the OBS overlay off the live `ui` dict. Best-effort: start() never
    raises (a busy port just means no overlay this run)."""

    def __init__(self, ui, port: int = DEFAULT_PORT, preset: dict = None, on_save=None, cfg=None, presets: dict = None, preset_name: str = '', on_persist=None):
        self.ui = ui
        self.port = int(port)
        self.preset = dict(DEFAULT_PRESET, **(preset or {}))
        self.on_save = on_save
        self.presets = dict(presets or {})
        self.preset_name = preset_name or ''
        self.on_persist = on_persist
        self.cfg = cfg
        self._bars = None
        self._httpd = None
        self._thread = None

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}/'

    def start(self) -> bool:
        try:
            self._httpd = ThreadingHTTPServer(('127.0.0.1', self.port), _Handler)
            self._httpd.ui = self.ui
            self._httpd.preset = self.preset
            self._httpd.on_save = self.on_save
            self._httpd.presets = self.presets
            self._httpd.preset_name = self.preset_name
            self._httpd.on_persist = self.on_persist
            if self.cfg is not None and self._bars is None:
                try:
                    from fh6_spotify.overlay_bars import OverlayBars
                    self._bars = OverlayBars(self.cfg, ui=self.ui)
                except Exception:
                    self._bars = None
            self._httpd.bars = self._bars
            self._thread = threading.Thread(target=self._httpd.serve_forever, name='segue-overlay', daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._httpd = None
            return False

    def set_preset(self, preset: dict):
        self.preset = dict(DEFAULT_PRESET, **(preset or {}))
        if self._httpd is not None:
            self._httpd.preset = self.preset

    def stop(self):
        if self._bars is not None:
            try:
                self._bars.stop()
            except Exception:
                pass
            self._bars = None
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None
