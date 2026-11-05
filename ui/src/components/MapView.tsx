import { useMemo, useRef, useState } from 'react';
import type { Model } from '../lib/model';
import { useLive } from '../lib/store';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';
import { Sev } from './Icons';

interface View { x: number; y: number; k: number }

export function MapView({ model, onOpenSld }: { model: Model | null; onOpenSld: (sub: string) => void }) {
  const live = useLive();
  const svg = useRef<SVGSVGElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const [sel, setSel] = useState<string | null>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const W = 1000, H = 640;

  const fit = useMemo(() => {
    if (!model) return { x: 0, y: 0, k: 1 };
    const b = model.bbox, pad = 50;
    const k = Math.min((W - pad * 2) / Math.max(b.x1, 1), (H - pad * 2) / Math.max(b.y1, 1));
    return { k, x: (W - b.x1 * k) / 2, y: (H - b.y1 * k) / 2 };
  }, [model]);
  const v = view.k === 1 && view.x === 0 && view.y === 0 ? fit : view;

  const worst = useMemo(() => {
    const m = new Map<string, number>();
    for (const a of live.alarms) { if (a.shelved || a.state === 'CLOSED') continue; m.set(a.sub, Math.min(m.get(a.sub) ?? 9, a.prio)); }
    return m;
  }, [live.alarms]);

  if (!model) return <div className="empty"><div className="skeleton" style={{ width: 260, margin: '0 auto 10px' }} />{t('map.loading')}</div>;

  const brkOpen = (b?: string) => !!b && live.pts.get(b + '.STATE')?.s === 1;
  const lines = model.branches.map(b => {
    const p = live.pts.get(b.id + '-F.P')?.v ?? 0, q = live.pts.get(b.id + '-F.Q')?.v ?? 0;
    const mva = Math.hypot(p, q), ld = b.rating > 0 ? mva / b.rating : 0;
    return { b, mva, ld, out: brkOpen(b.brkF) || brkOpen(b.brkT) };
  });

  const zoom = (f: number, cx = W / 2, cy = H / 2) => setView(s => {
    const cur = s.k === 1 && s.x === 0 && s.y === 0 ? fit : s;
    const k = Math.min(40, Math.max(fit.k * 0.8, cur.k * f));
    return { k, x: cx - (cx - cur.x) * (k / cur.k), y: cy - (cy - cur.y) * (k / cur.k) };
  });
  const toSvg = (e: { clientX: number; clientY: number }) => { const r = svg.current!.getBoundingClientRect(); return [(e.clientX - r.left) * (W / r.width), (e.clientY - r.top) * (H / r.height)]; };

  const selSub = sel ? model.subs.get(sel) : null;
  const vpu = (id: string) => { const s = model.subs.get(id)!; const x = live.pts.get(s.busNode + '.V')?.v; return x === undefined || !s.kv ? undefined : x / s.kv; };
  const unit = 1 / v.k;

  return (
    <div>
      <h1>{t('map.title')} <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t('map.synthetic')}</span></h1>
      <div className="mapwrap">
        <svg ref={svg} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t('map.title')}
          onWheel={e => { e.preventDefault(); const [cx, cy] = toSvg(e); zoom(e.deltaY < 0 ? 1.18 : 1 / 1.18, cx, cy); }}
          onPointerDown={e => { (e.target as Element).setPointerCapture?.(e.pointerId); drag.current = { x: e.clientX, y: e.clientY, vx: v.x, vy: v.y, moved: false }; }}
          onPointerMove={e => { const d = drag.current; if (!d) return; const r = svg.current!.getBoundingClientRect(); const dx = (e.clientX - d.x) * (W / r.width), dy = (e.clientY - d.y) * (H / r.height); if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true; if (d.moved) setView({ k: v.k, x: d.vx + dx, y: d.vy + dy }); }}
          onPointerUp={() => { drag.current = null; }}
          onKeyDown={e => { if (e.key === '+' || e.key === '=') zoom(1.3); if (e.key === '-') zoom(1 / 1.3); if (e.key === 'Escape') setSel(null); }} tabIndex={0}>
          <g transform={`translate(${v.x} ${v.y}) scale(${v.k})`}>
            {lines.map(({ b, mva, ld, out }) => {
              const a = model.subs.get(b.a)!, c = model.subs.get(b.b)!;
              const col = out ? 'var(--grid-dead)' : ld >= 1 ? 'var(--p1)' : ld >= 0.8 ? 'var(--p2)' : 'var(--grid-normal)';
              const w = out ? 1.2 : 1 + 2.6 * Math.min(1, mva / 220) + (ld >= 1 ? 1.2 : 0);
              const mx = (a.x + c.x) / 2, my = (a.y + c.y) / 2;
              return (
                <g key={b.id}>
                  <line x1={a.x} y1={a.y} x2={c.x} y2={c.y} stroke={col} strokeWidth={w} strokeDasharray={out ? '5 4' : undefined} vectorEffect="non-scaling-stroke" strokeLinecap="round"><title>{`${b.name}  ${n1(mva)} MVA  ${n1(ld * 100, 0)} %`}</title></line>
                  {out && <path d={`M${mx - 4 * unit} ${my - 4 * unit}l${8 * unit} ${8 * unit}m0 ${-8 * unit}l${-8 * unit} ${8 * unit}`} stroke="var(--p1)" strokeWidth={2} vectorEffect="non-scaling-stroke" fill="none" />}
                  {!out && ld >= 1 && <path d={`M${mx} ${my - 5 * unit}l${5 * unit} ${9 * unit}h${-10 * unit}z`} fill="var(--p1)" />}
                </g>
              );
            })}
            {[...model.subs.values()].map(s => {
              const volt = vpu(s.id), dead = volt !== undefined && volt < 0.2, wp = worst.get(s.id), r = (s.id === sel ? 7 : 4.6) * unit;
              const col = wp === 1 ? 'var(--p1)' : wp === 2 ? 'var(--p2)' : wp === 3 ? 'var(--p3)' : 'var(--muted)';
              return (
                <g key={s.id} onClick={() => { if (!drag.current?.moved) setSel(s.id); }} style={{ cursor: 'pointer' }}>
                  <circle cx={s.x} cy={s.y} r={r + 4 * unit} fill="transparent" />
                  <circle cx={s.x} cy={s.y} r={r} fill={dead ? 'var(--panel)' : 'var(--panel2)'} stroke={col} strokeWidth={wp ? 2.4 : 1.4} strokeDasharray={dead ? '2 2' : undefined} vectorEffect="non-scaling-stroke" />
                  {wp === 1 && <path d={`M${s.x + 5 * unit} ${s.y - 10 * unit}l${4 * unit} ${7 * unit}h${-8 * unit}z`} fill="var(--p1)" />}
                  {wp === 2 && <path d={`M${s.x + 5 * unit} ${s.y - 11 * unit}l${4 * unit} ${4 * unit}l${-4 * unit} ${4 * unit}l${-4 * unit} ${-4 * unit}z`} fill="var(--p2)" />}
                  {wp === 3 && <circle cx={s.x + 5 * unit} cy={s.y - 7 * unit} r={3.2 * unit} fill="var(--p3)" />}
                  {(v.k > 4.5 || s.id === sel) && <text x={s.x + 8 * unit} y={s.y + 3.5 * unit} fontSize={11 * unit} fill="var(--text)" style={{ pointerEvents: 'none' }}>{s.name.replace('Substation ', 'S')}</text>}
                </g>
              );
            })}
          </g>
        </svg>
        <div className="maptools">
          <button className="btn" aria-label={t('map.zoomIn')} onClick={() => zoom(1.4)}>+</button>
          <button className="btn" aria-label={t('map.zoomOut')} onClick={() => zoom(1 / 1.4)}>−</button>
          <button className="btn" aria-label={t('map.fit')} title={t('map.fit')} onClick={() => setView({ x: 0, y: 0, k: 1 })}>⤢</button>
        </div>
        <div className="maplegend" aria-label="legend">
          <div><svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" stroke="var(--grid-normal)" strokeWidth="3" /></svg> {t('legend.normal')}</div>
          <div><svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" stroke="var(--p2)" strokeWidth="3" /></svg> {t('legend.high')}</div>
          <div><svg width="26" height="10"><line x1="0" y1="5" x2="22" y2="5" stroke="var(--p1)" strokeWidth="4" /><path d="M23 1l3 8h-6z" fill="var(--p1)" /></svg> {t('legend.over')}</div>
          <div><svg width="26" height="10"><line x1="0" y1="5" x2="26" y2="5" stroke="var(--grid-dead)" strokeWidth="2" strokeDasharray="4 3" /></svg> {t('legend.out')}</div>
          <div><svg width="26" height="10"><circle cx="13" cy="5" r="4" fill="none" stroke="var(--muted)" strokeDasharray="2 2" /></svg> {t('legend.dead')}</div>
        </div>
        {selSub && (
          <div className="mapinfo" role="dialog" aria-label={selSub.name}>
            <div className="row" style={{ justifyContent: 'space-between' }}><strong>{selSub.name}</strong><button className="btn sm" aria-label={t('common.close')} onClick={() => setSel(null)}>✕</button></div>
            <div className="muted" style={{ fontSize: 12 }}>{t('map.region')}: {selSub.region} · {selSub.kv} kV</div>
            <div className="num" style={{ margin: '8px 0' }}>{t('map.voltage')}: {n1(vpu(selSub.id), 3)} pu</div>
            <div>{t('map.alarms')}: {worst.has(selSub.id) ? <Sev p={worst.get(selSub.id)!} /> : <span className="muted">{t('common.none')}</span>}</div>
            <button className="btn primary" style={{ marginTop: 10 }} onClick={() => onOpenSld(selSub.id)}>{t('map.open')}</button>
          </div>
        )}
      </div>
    </div>
  );
}
