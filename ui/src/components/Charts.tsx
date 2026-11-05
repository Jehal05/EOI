import { n1 } from '../lib/fmt';

const DASH = ['', '8 4', '2 3', '10 3 2 3'];
const COL = ['var(--accent)', 'var(--p2)', 'var(--p3)', 'var(--text)'];

export function Lines({ title, ts, series, unit, xUnit = 's', digits }: { title: string; ts: number[]; series: { name: string; y: number[]; i: number; dim?: boolean }[]; unit: string; xUnit?: string; digits?: number }) {
  const W = 760, H = 200, L = 52, B = 22, T = 8, R = 8;
  const all = series.flatMap(s => s.y);
  const lo0 = Math.min(...all), hi0 = Math.max(...all), pad = (hi0 - lo0) * 0.1 || 0.01;
  const lo = lo0 - pad, hi = hi0 + pad, t0 = ts[0] ?? 0, t1 = ts[ts.length - 1] || 1;
  const x = (v: number) => L + ((v - t0) / (t1 - t0 || 1)) * (W - L - R), y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const ticks = [0, 0.5, 1].map(f => lo + f * (hi - lo));
  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="muted" style={{ fontSize: 12 }}>{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${series.map(s => s.name).join(', ')}`} style={{ width: '100%', height: 'auto', maxHeight: 220 }}>
        {ticks.map(v => <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--line)" /><text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{n1(v, digits ?? (unit === 'Hz' ? 3 : 0))}</text></g>)}
        {series.map(s => <path key={s.name} d={s.y.map((v, k) => `${k ? 'L' : 'M'}${x(ts[k]).toFixed(1)},${y(v).toFixed(1)}`).join('')} fill="none" stroke={COL[s.i % 4]} strokeWidth={s.dim ? 1.2 : 2.2} strokeDasharray={s.dim ? '1 4' : DASH[s.i % 4]} opacity={s.dim ? 0.8 : 1} />)}
        <text x={L} y={H - 4} fontSize="11" fill="var(--muted)">{n1(ts[0] ?? 0, xUnit === 's' ? 0 : 2)} {xUnit}</text><text x={W - R} y={H - 4} fontSize="11" fill="var(--muted)" textAnchor="end">{n1(t1, xUnit === 's' ? 0 : 2)} {xUnit}</text>
      </svg>
    </figure>
  );
}

