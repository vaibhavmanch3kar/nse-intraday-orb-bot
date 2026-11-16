"""
Interactive charts for the dashboard: one small script, no outside libraries (works offline).

The server puts each chart's numbers in a data-chart attribute next to a static SVG (shown if the
script can't run). The script redraws the chart at the real on-screen size (labels stay readable on
a phone, lines never stretch), in the page's light or dark colours, and adds:
  - a crosshair and a value box on hover, touch (tap) or the arrow keys,
  - range buttons (1W, 1M, 3M, All) on charts that have dates.
Chart kinds: 'line' (equity), 'bars' (P&L per trade or per day), 'candles' (one trade's day).
Display only: it reads nothing but the numbers already on the page.
"""

import html
import json


def container(data, fallback, label, cls=""):
    """<div class='chart'> holding the chart's numbers (JSON) and a static fallback picture."""
    payload = html.escape(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return (f"<div class='chart {cls}' data-chart='{payload}' role='img' tabindex='0' "
            f"aria-label='{html.escape(label)}. Use the left and right arrow keys to read values.'>{fallback}</div>")


CSS = """
.chart{position:relative;outline:none;border-radius:10px}
.chart:focus-visible{box-shadow:0 0 0 2px var(--accent)}
.chart svg{display:block;width:100%;touch-action:pan-y}
.chart .ctip{position:absolute;top:4px;pointer-events:none;background:var(--surface-2);color:var(--text);
 border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:12px;line-height:1.35;box-shadow:var(--shadow);
 white-space:nowrap;z-index:2;display:none}
.chart .cranges{display:flex;gap:4px;justify-content:flex-end;margin:0 0 6px}
.chart .cranges button{min-height:28px;padding:2px 10px;margin:0;font-size:12px;background:var(--surface-2);color:var(--muted);
 border:1px solid var(--border);border-radius:7px}
.chart .cranges button.on{color:var(--text);border-color:var(--accent)}
"""

JS = r"""
(function(){
  var NS='http://www.w3.org/2000/svg';
  function cv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()||'#888'}
  function money(v,dp){dp=dp==null?0:dp;return (v<0?'-':'')+'₹'+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:dp,maximumFractionDigits:dp})}
  function signed(v){return (v>0?'▲ +':v<0?'▼ ':'')+money(v,2)}
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
  function mk(tag,a,parent){var e=document.createElementNS(NS,tag);for(var k in a)e.setAttribute(k,a[k]);if(parent)parent.appendChild(e);return e}
  function txt(parent,x,y,s,anchor,col,size){var t=mk('text',{x:x,y:y,'font-size':size||12,'text-anchor':anchor||'start',fill:col||cv('--muted')},parent);t.textContent=s;return t}
  var RANGES=[['1W',7],['1M',31],['3M',92],['All',0]];

  function visible(box){
    var d=box._d, r=box._range||0, pts=d.pts||[];
    if(!d.ranges||!r||!pts.length)return {pts:pts,base:d.base};
    var last=new Date(pts[pts.length-1][2]); var from=new Date(last.getTime()-r*864e5);
    var i=0; while(i<pts.length&&new Date(pts[i][2])<from)i++;
    if(i>=pts.length)i=pts.length-1;
    return {pts:pts.slice(i),base:i>0?pts[i-1][1]:d.base};
  }

  function ranges(box){
    if(!box._d.ranges||box.querySelector('.cranges'))return;
    var bar=document.createElement('div');bar.className='cranges';bar.setAttribute('role','group');bar.setAttribute('aria-label','Range');
    RANGES.forEach(function(r){var b=document.createElement('button');b.type='button';b.textContent=r[0];
      if(!r[1])b.className='on';
      b.onclick=function(){box._range=r[1];bar.querySelectorAll('button').forEach(function(x){x.classList.toggle('on',x===b)});draw(box)};
      bar.appendChild(b)});
    box.insertBefore(bar,box.firstChild);
  }

  function draw(box){
    var d=box._d, W=Math.max(240,Math.round(box.clientWidth)), H=d.h||(W<520?190:230);
    var old=box.querySelector('svg.live'); if(old)old.remove();
    box.querySelectorAll('svg:not(.live)').forEach(function(s){s.style.display='none'});
    var svg=mk('svg',{viewBox:'0 0 '+W+' '+H,width:W,height:H,class:'live','aria-hidden':'true'});
    box.appendChild(svg);
    var tip=box.querySelector('.ctip'); if(!tip){tip=document.createElement('div');tip.className='ctip';box.appendChild(tip)}
    tip.style.display='none'; box._i=null;   // a redraw (resize, theme, range) starts without an old value box
    var L=W<520?54:66, R=d.type==='candles'?(W<520?8:12):10, T=10, B=24;
    var up=cv('--up'), dn=cv('--dn'), mu=cv('--muted'), bd=cv('--border'), tx=cv('--text'), ac=cv('--accent');
    var hit=[], xs=[];
    if(d.type==='line'){
      var v=visible(box), pts=v.pts, base=v.base;
      if(!pts.length){txt(svg,L,H/2,'No data in this range','start');return}
      // equity after each trade starts from the balance before the first one ('start')
      var series=(d.from0&&base!=null)?[base].concat(pts.map(function(p){return p[1]})):pts.map(function(p){return p[1]});
      var off=series.length-pts.length, ref=base!=null?base:pts[0][1];
      var vals=series.concat([ref]);
      var lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals), pad=(hi-lo)*0.12||Math.max(Math.abs(hi)*0.002,1);
      lo-=pad;hi+=pad;
      var x=function(k){return L+k*(W-L-R)/Math.max(1,series.length-1)};
      var y=function(val){return T+(hi-val)/(hi-lo)*(H-T-B)};
      var col=series[series.length-1]>=ref?up:dn;
      [hi-pad,lo+pad].forEach(function(gv){mk('line',{x1:L,x2:W-R,y1:y(gv),y2:y(gv),stroke:bd},svg);txt(svg,L-6,y(gv)+4,money(gv),'end')});
      mk('line',{x1:L,x2:W-R,y1:y(ref),y2:y(ref),stroke:mu,'stroke-dasharray':'4 4'},svg);
      var line=series.map(function(val,k){return [x(k),y(val)]});
      if(d.dd){ // drawdown shading for equity curves: the area between the running high and the line
        var pk=-1e18, top=[], bot=[];
        series.forEach(function(val,k){pk=Math.max(pk,val);top.push(x(k)+','+y(pk));bot.push(x(k)+','+y(val))});
        mk('polygon',{points:top.concat(bot.reverse()).join(' '),fill:dn,opacity:.16},svg);
      } else {
        mk('path',{d:'M'+x(0)+','+(H-B)+' L'+line.map(function(q){return q[0]+','+q[1]}).join(' L')+' L'+x(series.length-1)+','+(H-B)+' Z',fill:col,opacity:.12},svg);
      }
      mk('polyline',{points:line.map(function(q){return q[0]+','+q[1]}).join(' '),fill:'none',stroke:col,'stroke-width':2,'stroke-linejoin':'round'},svg);
      txt(svg,x(0),H-6,off?(d.firstLabel||'start'):pts[0][0],'start');
      txt(svg,x(series.length-1),H-6,pts[pts.length-1][0],'end');
      pts.forEach(function(p,i){xs.push(x(i+off));hit.push([function(){var df=p[1]-ref;
        return '<b>'+esc(p[0])+'</b> · '+money(p[1],2)+' <span style="color:'+(df>=0?up:dn)+'">'+signed(df)+'</span>'+(p[3]?'<br>'+esc(p[3]):'')},
        y(p[1])])});
    } else if(d.type==='bars'){
      var pts=d.pts||[]; if(!pts.length){txt(svg,L,H/2,'No data yet');return}
      var m=Math.max.apply(null,pts.map(function(p){return Math.abs(p[1])}))||1;
      var y=function(val){return T+(m-val)/(2*m)*(H-T-B)}, slot=(W-L-R)/pts.length, bw=Math.max(1.5,Math.min(28,slot*0.7));
      mk('line',{x1:L,x2:W-R,y1:y(0),y2:y(0),stroke:mu},svg);
      txt(svg,L-6,y(m)+4,money(m),'end');txt(svg,L-6,y(-m)+4,money(-m),'end');
      pts.forEach(function(p,i){var cx=L+i*slot+slot/2, a=y(0), b=y(p[1]);
        mk('rect',{x:cx-bw/2,y:Math.min(a,b),width:bw,height:Math.max(1,Math.abs(b-a)),fill:p[1]>=0?up:dn,rx:Math.min(3,bw/4)},svg);
        xs.push(cx);hit.push([function(){return '<b>'+esc(p[0])+'</b> · <span style="color:'+(p[1]>=0?up:dn)+'">'+signed(p[1])+'</span>'+(p[2]?'<br>'+esc(p[2]):'')},b])});
      txt(svg,L,H-6,pts[0][0]);txt(svg,W-R,H-6,pts[pts.length-1][0],'end');
    } else if(d.type==='candles'){
      var bars=d.bars||[]; if(!bars.length){txt(svg,L,H/2,'No price data');return}
      var lv=[];bars.forEach(function(b){lv.push(b[2],b[3])});(d.lines||[]).forEach(function(l){lv.push(l.v)});
      (d.ema||[]).forEach(function(e){if(e!=null)lv.push(e)});
      var lo=Math.min.apply(null,lv), hi=Math.max.apply(null,lv), pad=(hi-lo)*0.06||1; lo-=pad;hi+=pad;
      var y=function(val){return T+(hi-val)/(hi-lo)*(H-T-B)}, slot=(W-L-R)/bars.length, bw=Math.max(2,Math.min(16,slot*0.62));
      var cx=function(i){return L+i*slot+slot/2};
      if(d.band){mk('rect',{x:L,y:y(d.band[1]),width:W-L-R,height:Math.max(1,y(d.band[0])-y(d.band[1])),fill:ac,opacity:.12},svg)}
      [hi-pad,lo+pad].forEach(function(gv){txt(svg,L-6,y(gv)+4,gv.toLocaleString('en-US',{maximumFractionDigits:2}),'end')});
      (d.lines||[]).forEach(function(l){var c=l.c==='dn'?dn:l.c==='up'?up:mu;
        mk('line',{x1:L,x2:W-R,y1:y(l.v),y2:y(l.v),stroke:c,'stroke-width':1.2,'stroke-dasharray':l.dash||'6 4'},svg);
        var t=txt(svg,L+4,y(l.v)-4,l.n+' '+l.v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}),'start',c,11)});
      bars.forEach(function(b,i){var c=b[4]>=b[1]?up:dn;
        mk('line',{x1:cx(i),x2:cx(i),y1:y(b[2]),y2:y(b[3]),stroke:c},svg);
        mk('rect',{x:cx(i)-bw/2,y:Math.min(y(b[1]),y(b[4])),width:bw,height:Math.max(1,Math.abs(y(b[1])-y(b[4]))),fill:c},svg);
        xs.push(cx(i));hit.push([function(){var e=d.ema&&d.ema[i]!=null?'<br>EMA '+d.ema[i].toFixed(2):'';
          return '<b>'+esc(b[0])+'</b> O '+b[1].toFixed(2)+' H '+b[2].toFixed(2)+' L '+b[3].toFixed(2)+' C '+b[4].toFixed(2)+e},y(b[4])])});
      if(d.ema){var e=[];d.ema.forEach(function(v,i){if(v!=null)e.push(cx(i)+','+y(v))});
        if(e.length>1)mk('polyline',{points:e.join(' '),fill:'none',stroke:cv('--trail'),'stroke-width':1.6},svg)}
      (d.marks||[]).forEach(function(mk2){var X=cx(mk2.i), Y=y(mk2.p), s=mk2.upward?1:-1;
        mk('polygon',{points:X+','+Y+' '+(X-7)+','+(Y+12*s)+' '+(X+7)+','+(Y+12*s),fill:mk2.k==='entry'?ac:tx,stroke:cv('--surface'),'stroke-width':1},svg)});
      [0,Math.floor(bars.length/2),bars.length-1].filter(function(v,i,a){return a.indexOf(v)===i}).forEach(function(i){
        txt(svg,cx(i),H-6,bars[i][0],'middle')});
    }
    // crosshair + value box: mouse, touch (tap) and arrow keys
    var cross=mk('line',{y1:T,y2:H-B,stroke:mu,'stroke-dasharray':'3 3',visibility:'hidden'},svg);
    var dot=mk('circle',{r:4.5,fill:tx,stroke:cv('--surface'),'stroke-width':2,visibility:'hidden'},svg);
    function show(i){if(i<0||i>=xs.length)return;box._i=i;var X=xs[i];
      cross.setAttribute('x1',X);cross.setAttribute('x2',X);cross.setAttribute('visibility','visible');
      dot.setAttribute('cx',X);dot.setAttribute('cy',hit[i][1]);dot.setAttribute('visibility','visible');
      tip.innerHTML=hit[i][0]();tip.style.display='block';
      var tw=tip.offsetWidth, left=X-tw/2; left=Math.max(4,Math.min(left,W-tw-4)); tip.style.left=left+'px';}
    function hide(){cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.style.display='none';box._i=null}
    function nearest(clientX){var r=svg.getBoundingClientRect(), X=(clientX-r.left)*W/r.width, best=0;
      for(var k=1;k<xs.length;k++)if(Math.abs(xs[k]-X)<Math.abs(xs[best]-X))best=k;return best}
    svg.addEventListener('pointermove',function(ev){if(ev.pointerType==='mouse')show(nearest(ev.clientX))});
    svg.addEventListener('pointerdown',function(ev){show(nearest(ev.clientX))});
    svg.addEventListener('pointerleave',function(ev){if(ev.pointerType==='mouse')hide()});
    box.onkeydown=function(ev){if(!xs.length)return;var i=box._i==null?xs.length-1:box._i;
      if(ev.key==='ArrowLeft'){show(Math.max(0,i-1));ev.preventDefault()}
      else if(ev.key==='ArrowRight'){show(Math.min(xs.length-1,i+1));ev.preventDefault()}
      else if(ev.key==='Escape'){hide()}};
    box.onblur=hide;
  }

  function init(root){
    (root||document).querySelectorAll('.chart[data-chart]').forEach(function(box){
      try{box._d=JSON.parse(box.getAttribute('data-chart'))}catch(e){return}
      ranges(box);draw(box);
      if(window.ResizeObserver&&!box._ro){var w=box.clientWidth;box._ro=new ResizeObserver(function(){
        if(Math.abs(box.clientWidth-w)>4){w=box.clientWidth;draw(box)}});box._ro.observe(box)}
    });
  }
  window.AyodhyaCharts={init:init};
  document.addEventListener('themechange',function(){document.querySelectorAll('.chart[data-chart]').forEach(function(b){if(b._d)draw(b)})});
  document.addEventListener('regionsupdated',function(){init(document)});
  document.addEventListener('click',function(ev){document.querySelectorAll('.chart .ctip').forEach(function(t){
    if(!t.parentNode.contains(ev.target))t.style.display='none'})});
  init(document);
})();
"""
