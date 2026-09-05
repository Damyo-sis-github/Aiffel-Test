"""로컬 정적 대시보드. **파일 하나**를 만들고, 브라우저로 열면 끝이다.

설계 결정
  - 서버를 띄우지 않는다. 로컬 웹서버는 포트를 열고, 그건 회사 네트워크에서
    감지된다. 파일 하나면 그런 노출이 없다 (§11.8).
  - 외부 CDN 을 쓰지 않는다. 오프라인에서 열려야 하고, 외부 요청은 흔적을 남긴다.
    차트는 바닐라 JS 로 SVG 를 직접 그린다.
  - 읽기 전용이다. 여기서 게이트를 통과시키거나 상태를 바꿀 수 있는 경로는 없다.

차트 선택 근거
  - NAV: 시간 추세 2계열 → 선그래프. 자본이 100배 다르므로 **이중 축을 쓰지 않고**
    시작점 100 으로 지수화해 한 축에 올린다.
  - 전략 기대값: 0을 기준으로 부호가 의미를 가짐 → 발산형(파랑↔빨강) 막대.
  - 테마·국가 랭킹: 크기 비교 → 단일 색상(순차) 가로 막대.
  - 헤드라인 수치: 단일 값 → 차트가 아니라 스탯 타일.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from app.paths import reports_dir
from app.reports.dashboard_data import build, to_json

# 검증된 팔레트 (dataviz 기준 인스턴스). 두 모드 모두 6개 검사를 통과함을 확인했다.
# 토큰은 :root 에 둔다. .dash 에만 두면 body 의 var(--ink) 가 해석되지 않아
# 다크모드에서 본문이 검은 글씨로 남아 보이지 않는다 (실제로 그랬다).
_CSS = """
:root{
  color-scheme:light;
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834;
  --pos:#2a78d6; --neg:#d03b3b;           /* 발산: 파랑 ↔ 빨강 */
  --seq-4:#3987e5;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --success-text:#006300;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --surface-1:#1a1a19; --page:#0d0d0d;
    --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926;
    --pos:#3987e5; --neg:#d03b3b;
    --seq-4:#3987e5;
    --success-text:#0ca30c;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926;
  --pos:#3987e5; --neg:#d03b3b;
  --seq-4:#3987e5;
  --success-text:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
.dash{max-width:1120px;margin:0 auto;padding:24px 20px 64px}
h1{font-size:22px;margin:0 0 2px;font-weight:650;letter-spacing:-.01em}
h2{font-size:15px;margin:0 0 12px;font-weight:600;color:var(--ink)}
.sub{color:var(--ink-2);font-size:13px;margin:0 0 20px}
.banner{border:1px solid var(--ring);border-left:3px solid var(--warn);
  background:var(--surface-1);border-radius:8px;padding:10px 14px;margin:0 0 18px;font-size:13px}
.banner.crit{border-left-color:var(--crit)}
.card{background:var(--surface-1);border:1px solid var(--ring);border-radius:10px;
  padding:18px;margin:0 0 18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:12px;margin:0 0 18px;
  align-items:stretch}
.kpi{background:var(--surface-1);border:1px solid var(--ring);border-radius:10px;
  padding:14px 16px;display:flex;flex-direction:column;min-width:0}
.kpi .l{font-size:12px;color:var(--ink-2);margin:0 0 6px}
.kpi .v{font-size:24px;font-weight:650;letter-spacing:-.02em;line-height:1.2;
  overflow-wrap:anywhere}
.kpi .v.hero{font-size:30px}
.kpi .u{font-size:14px;font-weight:500;color:var(--ink-2);margin-left:2px}
/* 보조 설명은 항상 muted. 상태색은 '값의 방향'을 말할 때만 쓴다 — 설명문에 색이
   붙으면 무엇이 나쁘다는 건지 읽는 사람이 헷갈린다. */
.kpi .d{font-size:11.5px;color:var(--muted);margin-top:auto;padding-top:5px;line-height:1.4}
.kpi .dv{font-size:12px;margin-top:4px;font-weight:600}
.kpi .dv.ok{color:var(--success-text)}
.kpi .dv.bad{color:var(--crit)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--grid);
  font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
th{color:var(--ink-2);font-weight:600;font-size:12px;white-space:nowrap}
td.dim{color:var(--muted)}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
  border:1px solid var(--ring);color:var(--ink-2);white-space:nowrap}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
  vertical-align:-1px;box-shadow:0 0 0 2px var(--surface-1)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink-2);margin:0 0 8px}
.status{display:flex;align-items:center;gap:9px;padding:7px 0;font-size:13px}
.status .ic{font-size:14px;width:18px;text-align:center}
.note{font-size:12px;color:var(--muted);margin-top:10px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.scroll{overflow-x:auto}
svg{display:block;width:100%;height:auto;overflow:visible}
/* SVG 색은 속성에 **굽지 않는다**. 구우면 렌더 시점의 토큰 값이 그대로 남아
   다크로 토글했을 때 라이트의 밝은 회색 그리드가 흰 선으로 튄다(실제로 그랬다).
   클래스로 주면 캐스케이드가 따라와 재렌더 없이 두 모드가 맞는다. */
.gridline{stroke:var(--grid)}
.axisline{stroke:var(--axis)}
.t-muted{fill:var(--muted)}
.t-ink{fill:var(--ink-2)}
.ring{stroke:var(--surface-1)}
.st-s1{stroke:var(--s1)}  .st-s2{stroke:var(--s2)}
.fl-s1{fill:var(--s1)}    .fl-s2{fill:var(--s2)}
.fl-pos{fill:var(--pos)}  .fl-neg{fill:var(--neg)}
.fl-seq{fill:var(--seq-4)}
.dot.s1{background:var(--s1)} .dot.s2{background:var(--s2)}
/* 랭킹 SVG 는 viewBox 가 좁아 카드 폭에 맞춰 늘리면 글자가 거대해진다. 상한을 준다. */
.rank svg{max-width:460px}
.tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .09s;
  background:var(--surface-1);border:1px solid var(--ring);border-radius:7px;
  padding:8px 11px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.14);z-index:9;
  font-variant-numeric:tabular-nums}
.toggle{position:absolute;top:24px;right:20px;background:var(--surface-1);
  border:1px solid var(--ring);border-radius:7px;padding:5px 11px;font-size:12px;
  color:var(--ink-2);cursor:pointer}
.wrap{position:relative}
details{margin-top:12px}
summary{cursor:pointer;font-size:12px;color:var(--ink-2)}
"""

_JS = r"""
const D = window.__DASH__;
const $ = (s,r=document)=>r.querySelector(s);
const NS='http://www.w3.org/2000/svg';
const el=(n,a={})=>{const e=document.createElementNS(NS,n);
  for(const k in a) e.setAttribute(k,a[k]); return e;};
const pct=(v,d=1)=>v==null?'n/a':(v*100).toFixed(d)+'%';
const num=(v,d=0)=>v==null?'n/a':v.toLocaleString('ko-KR',{maximumFractionDigits:d});
const slotCls=s=>s.slot===1?'s1':'s2';

/* ---- 툴팁: 마크보다 큰 히트 타깃, 크로스헤어 ---- */
const tip=$('#tip');
function showTip(e,html){tip.innerHTML=html;tip.style.opacity=1;
  const r=tip.getBoundingClientRect();
  let x=e.clientX+14, y=e.clientY-r.height-10;
  if(x+r.width>innerWidth-8) x=e.clientX-r.width-14;
  if(y<8) y=e.clientY+16;
  tip.style.left=x+'px'; tip.style.top=y+'px';}
function hideTip(){tip.style.opacity=0;}

/* ---- NAV: 2계열 선그래프. 지수화(시작=100)해 한 축에 ---- */
function navChart(){
  const host=$('#nav'); const d=D.nav;
  if(!d.dates.length){host.innerHTML='<p class="note">NAV 기록이 없습니다.</p>';return;}
  const W=820,H=260,P={t:14,r:56,b:26,l:44};
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  const all=d.series.flatMap(s=>s.values).filter(v=>v!=null);
  let lo=Math.min(...all,100), hi=Math.max(...all,100);
  const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
  const X=i=>P.l+(d.dates.length<2?iw/2:i/(d.dates.length-1)*iw);
  const Y=v=>P.t+ih-(v-lo)/(hi-lo)*ih;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img',
    'aria-label':'두 계좌 NAV 추이 (시작 100 기준 지수)'});

  /* 기준선 100 = 원금. 여기 위/아래가 손익이다.
     라벨은 왼쪽 안쪽에 둔다 — 오른쪽에 두면 계열 끝점 라벨과 겹친다(실제로 겹쳤다). */
  svg.appendChild(el('line',{x1:P.l,x2:P.l+iw,y1:Y(100),y2:Y(100),
    class:'axisline','stroke-width':1}));
  svg.appendChild(Object.assign(el('text',{x:P.l+6,y:Y(100)-6,
    class:'t-muted','font-size':11}),{textContent:'100 = 원금'}));

  /* 가로 그리드 — 해어라인, 실선, 후퇴색 */
  for(let k=0;k<=3;k++){
    const v=lo+(hi-lo)*k/3;
    svg.appendChild(el('line',{x1:P.l,x2:P.l+iw,y1:Y(v),y2:Y(v),
      class:'gridline','stroke-width':1}));
    svg.appendChild(Object.assign(el('text',{x:P.l-8,y:Y(v)+4,'text-anchor':'end',
      class:'t-muted','font-size':11}),{textContent:v.toFixed(1)}));
  }
  d.series.forEach(s=>{
    const k=slotCls(s);
    const pts=s.values.map((v,i)=>v==null?null:[X(i),Y(v)]).filter(Boolean);
    if(!pts.length) return;
    svg.appendChild(el('path',{d:'M'+pts.map(p=>p.join(' ')).join('L'),
      fill:'none',class:'st-'+k,'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
    const last=pts[pts.length-1];
    svg.appendChild(el('circle',{cx:last[0],cy:last[1],r:4.5,
      class:'fl-'+k+' ring','stroke-width':2}));                /* 표면 링 */
    /* 끝점 직접 라벨 — 텍스트는 잉크색, 계열색은 옆의 점이 담당 */
    svg.appendChild(Object.assign(el('text',{x:last[0]+9,y:last[1]+4,
      class:'t-ink','font-size':11}),
      {textContent:s.name+' '+s.values[s.values.length-1].toFixed(1)}));
  });
  /* 크로스헤어 + 툴팁 */
  const cross=el('line',{y1:P.t,y2:P.t+ih,class:'axisline',
    'stroke-width':1,opacity:0}); svg.appendChild(cross);
  const hit=el('rect',{x:P.l,y:P.t,width:iw,height:ih,fill:'transparent'});
  svg.appendChild(hit);
  hit.addEventListener('mousemove',e=>{
    const b=svg.getBoundingClientRect();
    const i=Math.max(0,Math.min(d.dates.length-1,
      Math.round((e.clientX-b.left)/b.width*W-P.l)/iw*(d.dates.length-1)));
    const k=Math.round(i);
    cross.setAttribute('x1',X(k));cross.setAttribute('x2',X(k));cross.setAttribute('opacity',1);
    showTip(e,`<b>${d.dates[k]}</b><br>`+d.series.map(s=>
      `<span class="dot ${slotCls(s)}"></span>${s.name} ${
      s.values[k]==null?'n/a':s.values[k].toFixed(2)}`).join('<br>'));
  });
  hit.addEventListener('mouseleave',()=>{cross.setAttribute('opacity',0);hideTip();});
  host.appendChild(svg);
  $('#navlegend').innerHTML=d.series.map(s=>
    `<span><span class="dot ${slotCls(s)}"></span>${
    s.name}${s.official?' (공식 판정)':''}</span>`).join('');
}

/* ---- 전략 기대값: 0 기준 발산형 막대 ---- */
function expectancyChart(){
  const host=$('#exp');
  const rows=D.strategies.filter(s=>s.expectancy!=null);
  if(!rows.length){host.innerHTML='<p class="note">청산 거래가 없어 기대값을 낼 수 없습니다.</p>';return;}
  const bh=26, W=820, H=rows.length*bh+30, L=170, R=86;
  const iw=W-L-R;
  const m=Math.max(...rows.map(r=>Math.abs(r.expectancy)))||0.01;
  const cx=L+iw/2, sc=(iw/2)/m;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img',
    'aria-label':'전략별 거래당 기대값'});
  svg.appendChild(el('line',{x1:cx,x2:cx,y1:6,y2:H-24,class:'axisline','stroke-width':1}));
  rows.forEach((r,i)=>{
    const y=8+i*bh, w=Math.abs(r.expectancy)*sc, up=r.expectancy>0;
    const x=up?cx:cx-w;
    /* 24px 이하 두께, 데이터 끝 4px 라운드 */
    const p=el('path',{d:roundedBar(x,y+3,Math.max(w,1.5),bh-10,up),
      class:up?'fl-pos':'fl-neg',opacity:r.meaningful?1:0.45});
    p.addEventListener('mousemove',e=>showTip(e,
      `<b>${r.strategy_id}</b> <span class="tag">${r.family}</span><br>`+
      `기대값 ${pct(r.expectancy,3)} · 승률 ${pct(r.win_rate)}<br>`+
      `n=${r.n} · 95% CI [${pct(r.ci_low)}, ${pct(r.ci_high)}]<br>`+
      (r.meaningful?'':'<i>n&lt;30 — 표본이 적어 아직 의미 없음</i>')));
    p.addEventListener('mouseleave',hideTip);
    svg.appendChild(p);
    svg.appendChild(Object.assign(el('text',{x:L-10,y:y+bh/2+1,'text-anchor':'end',
      class:'t-ink','font-size':12}),{textContent:r.strategy_id}));
    /* 값 라벨은 오른쪽 고정 열에 정렬한다. 막대 끝에 붙이면 0 근처의 짧은 음수 막대에서
       왼쪽 전략명 위로 넘어가 겹친다(실제로 겹쳤다). */
    svg.appendChild(Object.assign(el('text',{x:W-8,y:y+bh/2+1,'text-anchor':'end',
      class:'t-ink','font-size':11}),
      {textContent:pct(r.expectancy,2)+(r.meaningful?'':' *')}));
  });
  svg.appendChild(Object.assign(el('text',{x:cx,y:H-6,'text-anchor':'middle',
    class:'t-muted','font-size':11}),{textContent:'← 손실   0   이익 →'}));
  host.appendChild(svg);
}
function roundedBar(x,y,w,h,up){
  const r=Math.min(4,w,h/2);
  return up
   ? `M${x} ${y}H${x+w-r}Q${x+w} ${y} ${x+w} ${y+r}V${y+h-r}Q${x+w} ${y+h} ${x+w-r} ${y+h}H${x}Z`
   : `M${x+w} ${y}H${x+r}Q${x} ${y} ${x} ${y+r}V${y+h-r}Q${x} ${y+h} ${x+r} ${y+h}H${x+w}Z`;
}

/* ---- 랭킹: 단일 색상 가로 막대 (크기 비교) ---- */
function rankChart(id,rows,labelKey,extraKey){
  const host=$(id);
  if(!rows||!rows.length){host.innerHTML='<p class="note">아직 랭킹이 없습니다.</p>';return;}
  const bh=24,W=420,H=rows.length*bh+8,L=118,R=44,iw=W-L-R;
  const m=Math.max(...rows.map(r=>r.score||0))||1;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'랭킹'});
  rows.forEach((r,i)=>{
    const y=i*bh, w=Math.max((r.score||0)/m*iw,2);
    const b=el('path',{d:roundedBar(L,y+4,w,bh-9,true),class:'fl-seq'});
    b.addEventListener('mousemove',e=>showTip(e,
      `<b>${r[labelKey]}</b><br>점수 ${num(r.score,1)}`+
      (extraKey&&r[extraKey]!=null?`<br>${extraKey} ${r[extraKey]}`:'')));
    b.addEventListener('mouseleave',hideTip);
    svg.appendChild(b);
    svg.appendChild(Object.assign(el('text',{x:L-8,y:y+bh/2+2,'text-anchor':'end',
      class:'t-ink','font-size':12}),{textContent:String(r[labelKey]).slice(0,14)}));
    svg.appendChild(Object.assign(el('text',{x:L+w+7,y:y+bh/2+2,
      class:'t-muted','font-size':11}),{textContent:num(r.score,0)}));
  });
  host.appendChild(svg);
}

function boot(){
  navChart(); expectancyChart();
  rankChart('#themes',D.rankings.themes,'theme','n');
  rankChart('#countries',D.rankings.countries,'name');
  rankChart('#screener',D.rankings.screener,'symbol','theme');
}
$('#theme-toggle').addEventListener('click',()=>{
  const cur=document.documentElement.getAttribute('data-theme');
  const next=cur==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',next);
  document.querySelectorAll('#nav,#exp,#themes,#countries,#screener')
    .forEach(n=>n.innerHTML='');
  boot();
});
boot();
"""


def _kpi(label: str, value: str, note: str = "", delta: str = "",
         cls: str = "", hero: bool = False) -> str:
    """스탯 타일. delta 는 값의 방향(색 있음), note 는 설명문(항상 muted)."""
    dv = f'<div class="dv {cls}">{delta}</div>' if delta else ""
    d = f'<div class="d">{note}</div>' if note else ""
    return (f'<div class="kpi"><div class="l">{label}</div>'
            f'<div class="v{" hero" if hero else ""}">{value}</div>{dv}{d}</div>')


def _pct(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def _num(v) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def _won(v) -> str:
    """큰 금액은 압축한다. 원 단위 그대로 두면 타일 폭을 넘어 잘린다(실제로 잘렸다)."""
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:,.3f}<span class='u'>억</span>"
    if a >= 1e4:
        return f"{v / 1e4:,.0f}<span class='u'>만</span>"
    return f"{v:,.0f}<span class='u'>원</span>"


def _status_row(icon: str, text: str, detail: str = "") -> str:
    tail = f' <span class="tag">{detail}</span>' if detail else ""
    return f'<div class="status"><span class="ic">{icon}</span><span>{text}{tail}</span></div>'


def render(data: dict) -> str:
    h = data["headline"]
    ops = data["operations"]
    n_closed = h["closed_trades"]
    official_gap = max(0, h["official_threshold"] - n_closed)

    banners = []
    if data["synthetic"]:
        banners.append('<div class="banner">⚠️ <b>합성 데이터입니다.</b> 실제 시장 결과가 아닙니다. '
                       '파이프라인 동작 확인용이며, 이 숫자로 어떤 판단도 하지 마십시오.</div>')
    if ops["killswitch"]["tripped"]:
        banners.append(f'<div class="banner crit">🛑 {ops["killswitch"]["message"]}</div>')
    if ops["alerts_console_only"]:
        banners.append('<div class="banner">📵 알림이 <b>콘솔 전용 모드</b>입니다. 텔레그램으로 가지 않고 '
                       '<code>reports/alerts.log</code> 에만 남습니다.</div>')
    if ops["alerts_pending"]:
        banners.append(f'<div class="banner crit">⚠️ 사람에게 닿지 못한 알림 {ops["alerts_pending"]}건. '
                       '<code>quant healthcheck</code> 로 확인하십시오.</div>')

    # 헤드라인 — 단일 값이므로 차트가 아니라 스탯 타일
    ret = None if h["nav"] is None or not h["capital"] else h["nav"] / h["capital"] - 1
    dd = h["drawdown"] or 0
    kpis = [
        _kpi(f'{data["official_account"]} 평가액', _won(h["nav"]),
             note=f'원금 {_won(h["capital"])}',
             delta=(f'원금 대비 {ret * 100:+.2f}%' if ret is not None else ""),
             cls="ok" if (ret or 0) > 0 else "bad" if ret is not None else "", hero=True),
        _kpi("낙폭", _pct(dd), note="킬스위치 한도 −15%",
             delta="한도 근접" if abs(dd) > 0.10 else "", cls="bad" if abs(dd) > 0.10 else ""),
        _kpi("거래 승률 W_trade", _pct(h["win_rate"]) if n_closed else "n/a",
             note=(f'n={n_closed} · 95% CI [{_pct(h["win_ci"][0])}, {_pct(h["win_ci"][1])}]'
                   if n_closed else "청산 거래 없음"),
             delta="표본 부족" if 0 < n_closed < 30 else "",
             cls="bad" if 0 < n_closed < 30 else ""),
        _kpi("거래당 기대값 E", _pct(h["expectancy"], 3) if n_closed else "n/a",
             note="1차 게이트 — 0보다 커야 합니다",
             delta=("통과" if (h["expectancy"] or 0) > 0 else "미달") if n_closed else "",
             cls="ok" if (h["expectancy"] or 0) > 0 else "bad" if n_closed else ""),
        _kpi("명목 익스포저", _pct(h["gross_notional_pct"]), note="상한 150%"),
        _kpi("공식 판정까지", f"{official_gap}건" if official_gap else "도달",
             note=f'ACC_L 청산 {n_closed} / 100건'),
    ]

    dev = ops["device"]
    dev_icon = {0: "✅", 1: "✅", 2: "⚠️", 3: "🛑", 4: "🛑"}.get(dev.get("level"), "❔")
    integ = ops["integrity"]
    integ_txt = ", ".join(f"{k} {v}건" for k, v in integ.items()) if integ else "이벤트 없음"
    status = [
        _status_row("🛑" if ops["killswitch"]["tripped"] else "✅",
                    "킬스위치 " + ("발동" if ops["killswitch"]["tripped"] else "정상")),
        _status_row(dev_icon, f'기기 가시성 레벨 {dev.get("level", "?")}',
                    dev.get("label") or ("미검사" if not dev.get("checked") else "")),
        _status_row("📋", f"데이터 무결성 (최근 30일): {integ_txt}"),
        _status_row("🔬", f'진화 트리거 {len(ops["triggers"])}건'
                    if ops["triggers"] else "진화 트리거 없음"),
    ]

    strat_rows = "".join(
        f'<tr><td>{s["strategy_id"]}</td><td><span class="tag">{s["family"]}</span></td>'
        f'<td><span class="tag">{s["status"]}</span></td>'
        f'<td>{s["n"]}</td>'
        f'<td class="{"" if s["meaningful"] else "dim"}">{_pct(s["win_rate"])}</td>'
        f'<td class="dim">{_pct(s["ci_low"])} ~ {_pct(s["ci_high"])}</td>'
        f'<td class="{"" if s["meaningful"] else "dim"}">{_pct(s["expectancy"], 3)}</td>'
        f'<td class="dim">{_pct(s["worst"], 1)}</td>'
        f'<td>{_pct(s["allocation_pct"])}</td></tr>'
        for s in data["strategies"]
    ) or '<tr><td colspan="9" class="dim">전략 기록이 없습니다.</td></tr>'

    if data["predictions"]:
        pred = "".join(
            f'<tr><td>h={p["horizon"]}</td><td>{p["n"]}</td><td>{_pct(p["w_pred"])}</td>'
            f'<td class="dim">{_pct(p["ci_low"])} ~ {_pct(p["ci_high"])}</td>'
            f'<td class="dim">{"미계산" if p["b_pred"] is None else _pct(p["b_pred"])}</td></tr>'
            for p in data["predictions"])
        pred_block = (
            '<table><thead><tr><th>호라이즌</th><th>n</th><th>W_pred</th><th>95% CI</th>'
            '<th>B_pred (우연 기준선)</th></tr></thead><tbody>' + pred + "</tbody></table>"
            '<p class="note">B_pred 없이 W_pred 만 보면 안 됩니다. 상승장에선 아무거나 찍어도 '
            '60%가 맞습니다. 판단은 <b>W_pred − B_pred</b> 로 합니다 (§6.6).</p>')
    else:
        pred_block = '<p class="note">아직 채점된 예측이 없습니다.</p>'

    runs = "".join(
        f'<tr><td>{r.get("run_date")}</td><td>{r.get("task")}</td>'
        f'<td>{r.get("mode")}</td><td>{r.get("status")}</td>'
        f'<td class="dim">{r.get("result_hash")}</td></tr>'
        for r in ops["runs"]) or '<tr><td colspan="5" class="dim">실행 기록 없음</td></tr>'

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>자가 발전형 시장 리서치 — {data["as_of"]}</title>
<style>{_CSS}</style></head>
<body><div class="dash wrap">
<button class="toggle" id="theme-toggle">라이트/다크</button>
<h1>자가 발전형 시장 리서치</h1>
<p class="sub">기준일 {data["as_of"]} · 생성 {data["generated_at"]} ·
 데이터 소스 {data["sources"]} · <b>페이퍼 트레이딩 전용 · 실계좌 주문 없음</b></p>
{"".join(banners)}

<div class="kpis">{"".join(kpis)}</div>

<div class="card">
  <h2>두 계좌 NAV</h2>
  <div class="legend" id="navlegend"></div>
  <div id="nav"></div>
  <p class="note">{data["nav"].get("note", "")}</p>
</div>

<div class="card">
  <h2>전략별 거래당 기대값</h2>
  <div id="exp"></div>
  <p class="note">0 오른쪽이 이익, 왼쪽이 손실입니다. 흐린 막대와 <b>*</b> 는 n&lt;30 —
   표본이 적어 아직 의미가 없습니다. 공식 판정은 ACC_L 청산 100거래부터입니다 (§1.2).</p>
  <details><summary>표로 보기</summary>
  <div class="scroll"><table><thead><tr>
    <th>전략</th><th>계열</th><th>상태</th><th>n</th><th>W_trade</th>
    <th>95% CI</th><th>기대값 E</th><th>최악 거래</th><th>배분</th>
  </tr></thead><tbody>{strat_rows}</tbody></table></div></details>
</div>

<div class="grid2">
  <div class="card rank"><h2>테마 랭킹 (1계층)</h2><div id="themes"></div></div>
  <div class="card rank"><h2>국가·지역 랭킹 (2계층)</h2><div id="countries"></div></div>
</div>
<div class="card rank"><h2>종목 스크리너 (3계층)</h2><div id="screener"></div>
  <p class="note">상위 테마 × 상위 국가 교집합에서 뽑은 후보입니다.
   <b>매수 지시가 아니라</b> 전략의 유니버스 편향 입력입니다 (§6.5).</p></div>

<div class="card"><h2>예측 적중률</h2>{pred_block}</div>

<div class="card"><h2>운영 상태</h2>{"".join(status)}
  <details><summary>최근 실행 기록</summary>
  <div class="scroll"><table><thead><tr><th>날짜</th><th>작업</th><th>모드</th>
    <th>상태</th><th>결과 해시</th></tr></thead><tbody>{runs}</tbody></table></div></details>
</div>

<p class="note">이 문서는 투자 자문이 아닙니다. 실전 판단과 손실 책임은 본인에게 있습니다.
 이 저장소에는 실계좌 주문 코드가 존재하지 않습니다 (§15).</p>
</div>
<div class="tip" id="tip"></div>
<script>window.__DASH__={to_json(data)};</script>
<script>{_JS}</script>
</body></html>"""


def write(as_of: dt.date | None = None, path: Path | None = None) -> Path:
    data = build(as_of)
    p = path or (reports_dir() / "dashboard.html")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(data), encoding="utf-8")
    return p
