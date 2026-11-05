import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api';
import { useLive } from '../lib/store';
import { t } from '../lib/i18n';
import { clock, n1 } from '../lib/fmt';

interface TP { t: number; min: number; avg: number; max: number; n: number }
interface Trend { tag: string; resolution: string; points: TP[]; query_ms: number }
interface PL { points: { tag: string; low: number | null; high: number | null; unit: string; kind: string }[] }

const RANGES = [['15m', 15 * 60e3], ['1h', 3600e3], ['6h', 6 * 3600e3], ['24h', 24 * 3600e3]] as const;

export function TrendView({ model }: { model: string }) {
  const live = useLive();
  const [tag, setTag] = useState('BR0020-F.P'), [q, setQ] = useState(''), [range, setRange] = useState<number>(15 * 60e3);
  const [data, setData] = useState<Trend | null>(null), [err, setErr] = useState(''), [hover, setHover] = useState<number | null>(null);
  const [limits, setLimits] = useState<Map<string, { low: number | null; high: number | null; unit: string }>>(new Map());
  const alive = useRef(true);

  useEffect(() => { api<PL>(`/cim/models/${model}/pointlist`).then(p => setLimits(new Map(p.points.map(x => [x.tag, { low: x.low, high: x.high, unit: x.unit }])))).catch(() => undefined); }, [model]);
  useEffect(() => {
    alive.current = true;
    const load = async () => {
      const to = Date.now();
      try { const d = await api<Trend>(`/hist/v1/trend?tag=${encodeURIComponent(tag)}&from=${to - range}&to=${to}&max_points=500`); if (alive.current) { setData(d); setErr(''); } }
      catch (e) { if (alive.current) setErr((e as Error).message); }
    };
    void load();
    const i = setInterval(load, 5000);
    return () => { alive.current = false; clearInterval(i); };
  }, [tag, range]);

  const suggestions = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return [];
    const out: string[] = [];
    for (const k of live.pts.keys()) { if (k.toLowerCase().includes(s)) { out.push(k); if (out.length >= 10) break; } }
    return out;
  }, [q, live.pts]);

  const W = 900, H = 320, L = 56, R = 12, T = 12, B = 28;
  const pts = data?.points ?? [];
  const lim = limits.get(tag);
  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { lo = Math.min(lo, p.min); hi = Math.max(hi, p.max); }
  if (lim?.high != null && hi !== -Infinity && lim.high < hi * 2 + 10) hi = Math.max(hi, lim.high);
  if (lim?.low != null && lo !== Infinity && lim.low > lo * 2 - 10) lo = Math.min(lo, lim.low);
  if (!isFinite(lo) || lo === hi) { lo = (isFinite(lo) ? lo : 0) - 1; hi = (isFinite(hi) ? hi : 0) + 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const tMin = Date.now() - range, tMax = Date.now();
  const X = (x: number) => L + ((x - tMin) / (tMax - tMin)) * (W - L - R), Y = (y: number) => T + (1 - (y - lo) / (hi - lo)) * (H - T - B);
  const band = pts.length ? `M${pts.map(p => `${X(p.t)},${Y(p.max)}`).join('L')}L${[...pts].reverse().map(p => `${X(p.t)},${Y(p.min)}`).join('L')}Z` : '';
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.t)},${Y(p.avg)}`).join('');
  const ticks = Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4);
  const hp = hover !== null ? pts.reduce((b, p) => (Math.abs(p.t - hover) < Math.abs(b.t - hover) ? p : b), pts[0]) : null;

  return (
    <div>
      <h1>{t('trend.title')}</h1>
      <div className="row" style={{ marginBottom: 10, position: 'relative' }}>
        <input type="search" placeholder={t('trend.search')} aria-label={t('trend.search')} value={q} onChange={e => setQ(e.target.value)} style={{ width: 280 }} />
        <span className="mono">{tag}</span>
        <span className="spacer" />
        <div className="seg" role="group" aria-label={t('trend.range')}>{RANGES.map(([n, ms]) => <button key={n} aria-pressed={range === ms} onClick={() => setRange(ms)}>{n}</button>)}</div>
      </div>
      {suggestions.length > 0 && <div className="row" style={{ marginBottom: 8 }}>{suggestions.map(s => <button key={s} className="btn sm mono" onClick={() => { setTag(s); setQ(''); }}>{s}</button>)}</div>}
      <div className="card">
        {err && <div className="err">{err}</div>}
        {pts.length === 0 ? <div className="empty">{t('trend.nodata')}</div> : (
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`${t('trend.title')} ${tag}`} onPointerMove={e => { const r = e.currentTarget.getBoundingClientRect(); setHover(tMin + ((e.clientX - r.left) / r.width * W - L) / (W - L - R) * (tMax - tMin)); }} onPointerLeave={() => setHover(null)}>
            {ticks.map((v, i) => <g key={i}><line x1={L} x2={W - R} y1={Y(v)} y2={Y(v)} stroke="var(--line)" /><text x={L - 6} y={Y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)" className="mono">{n1(v, Math.abs(hi - lo) < 5 ? 2 : 1)}</text></g>)}
            {[0, 1, 2, 3, 4].map(i => { const x = tMin + ((tMax - tMin) * i) / 4; return <text key={i} x={X(x)} y={H - 8} textAnchor="middle" fontSize="11" fill="var(--muted)" className="mono">{clock(x)}</text>; })}
            {lim?.high != null && Y(lim.high) > T && Y(lim.high) < H - B && <g><line x1={L} x2={W - R} y1={Y(lim.high)} y2={Y(lim.high)} stroke="var(--p2)" strokeDasharray="6 4" /><text x={W - R - 4} y={Y(lim.high) - 4} textAnchor="end" fontSize="11" fill="var(--p2)">▲ {t('trend.limits')} {n1(lim.high)}</text></g>}
            {lim?.low != null && Y(lim.low) > T && Y(lim.low) < H - B && <g><line x1={L} x2={W - R} y1={Y(lim.low)} y2={Y(lim.low)} stroke="var(--p2)" strokeDasharray="6 4" /><text x={W - R - 4} y={Y(lim.low) + 12} textAnchor="end" fontSize="11" fill="var(--p2)">▼ {n1(lim.low)}</text></g>}
            <path d={band} fill="var(--accent)" opacity="0.18" />
            <path d={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
            {hp && <g><line x1={X(hp.t)} x2={X(hp.t)} y1={T} y2={H - B} stroke="var(--muted)" /><circle cx={X(hp.t)} cy={Y(hp.avg)} r="4" fill="var(--accent)" /></g>}
          </svg>)}
        <div className="row mono" style={{ minHeight: 22, fontSize: 12 }}>
          {hp ? <><span>{clock(hp.t)}</span><span>{t('trend.min')} {n1(hp.min, 2)}</span><span>{t('trend.avg')} {n1(hp.avg, 2)}</span><span>{t('trend.max')} {n1(hp.max, 2)}</span><span className="muted">n={hp.n}</span></> :
            data && <span className="muted">{t('trend.resolution')}: {data.resolution} · {data.points.length} pts · {n1(data.query_ms, 1)} ms {lim?.unit ? `· ${lim.unit}` : ''}</span>}
        </div>
      </div>
    </div>
  );
}
