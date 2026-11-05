import { useEffect, useMemo, useState } from 'react';
import { api, post, atLeast, getSession } from '../lib/api';
import { useLive } from '../lib/store';
import { t, type Key } from '../lib/i18n';
import { clock, n1 } from '../lib/fmt';

interface Pt { t: number; p50: number; lo80: number; hi80: number; lo95: number; hi95: number }
interface Res {
  model: string; seasonality: string; history_hours: number; points: Pt[]; history: { t: number; v: number }[]; warnings: string[]; step_min: number;
  backtest: { n_origins: number; mae: Record<string, number>; skill_vs_persistence: Record<string, number>; coverage: Record<string, number>; nominal: Record<string, number> };
}
const SYN: Record<string, { tag: string; kind: 'load' | 'solar' | 'wind'; label: Key }> = {
  'syn.load': { tag: 'SYN.LOAD.P', kind: 'load', label: 'fc.syn.load' }, 'syn.solar': { tag: 'SYN.SOLAR.P', kind: 'solar', label: 'fc.syn.solar' }, 'syn.wind': { tag: 'SYN.WIND.P', kind: 'wind', label: 'fc.syn.wind' },
};

function Chart({ r }: { r: Res }) {
  const W = 760, H = 240, L = 46, B = 24, T = 8, R = 8;
  const hist = r.history, pts = r.points;
  const t0 = hist[0]?.t ?? pts[0].t, t1 = pts[pts.length - 1].t;
  const ys = [...hist.map(h => h.v), ...pts.flatMap(p => [p.lo95, p.hi95])];
  const lo = Math.min(...ys), hi = Math.max(...ys), pad = (hi - lo) * 0.08 || 1;
  const x = (tt: number) => L + ((tt - t0) / (t1 - t0 || 1)) * (W - L - R);
  const y = (v: number) => T + (1 - (v - (lo - pad)) / (hi - lo + 2 * pad)) * (H - T - B);
  const line = (a: [number, number][]) => a.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`).join('');
  const band = (lk: 'lo80' | 'lo95', hk: 'hi80' | 'hi95') => line(pts.map(p => [x(p.t), y(p[hk])])) + line([...pts].reverse().map(p => [x(p.t), y(p[lk])])).replace('M', 'L') + 'Z';
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => lo - pad + f * (hi - lo + 2 * pad));
  const nowX = x(hist[hist.length - 1]?.t ?? pts[0].t);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t('fc.chart')} style={{ width: '100%', height: 'auto', maxHeight: 280 }}>
      {ticks.map(v => <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth="1" /><text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{n1(v, 0)}</text></g>)}
      <path d={band('lo95', 'hi95')} fill="var(--accent)" opacity="0.18" />
      <path d={band('lo80', 'hi80')} fill="var(--accent)" opacity="0.32" />
      <line x1={nowX} x2={nowX} y1={T} y2={H - B} stroke="var(--muted)" strokeDasharray="2 3" />
      <path d={line(hist.map(h => [x(h.t), y(h.v)]))} fill="none" stroke="var(--text)" strokeWidth="1.6" />
      <path d={line(pts.map(p => [x(p.t), y(p.p50)]))} fill="none" stroke="var(--accent)" strokeWidth="2.4" strokeDasharray="7 4" />
      <text x={L} y={H - 6} fontSize="11" fill="var(--muted)">{clock(t0)}</text>
      <text x={W - R} y={H - 6} fontSize="11" fill="var(--muted)" textAnchor="end">{clock(t1)}</text>
    </svg>
  );
}

export function ForecastView() {
  const live = useLive();
  const [series, setSeries] = useState('syn.load');
  const [horizon, setHorizon] = useState(120);
  const [step, setStep] = useState(5);
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ai, setAi] = useState<boolean | null>(null);
  const [reason, setReason] = useState('');
  const isAdmin = atLeast(getSession(), 'admin');
  const loadTags = useMemo(() => [...live.pts.keys()].filter(k => /^L\d+\.P$/.test(k)).sort(), [live.version]);
  const refresh = () => api<{ enabled: boolean }>('/api/ai').then(r => setAi(r.enabled)).catch(() => setAi(null));
  useEffect(() => { refresh(); }, []);
  const run = () => {
    setBusy(true); setErr(null);
    const s = SYN[series];
    const body = s ? { tags: [s.tag], mode: 'single', kind: s.kind } : { tags: loadTags, mode: 'sum', kind: 'load' };
    post<Res>('/hist/v1/forecast', { ...body, horizon_min: horizon, step_min: step, history_hours: 72 })
      .then(setRes).catch(e => { setRes(null); setErr((e as Error).message); }).finally(() => { setBusy(false); refresh(); });
  };
  const toggle = () => {
    post('/api/ai/switch', { enabled: !ai, reason }).then(() => { setReason(''); refresh(); }).catch(e => setErr((e as Error).message));
  };
  const b = res?.backtest;
  return (
    <div>
      <h1>{t('fc.title')} {busy && <span className="muted">{t('fc.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 16, flexWrap: 'wrap' }}>
        <label>{t('fc.series')} <select value={series} onChange={e => setSeries(e.target.value)}>
          <option value="live">{t('fc.totalload')}</option>
          {Object.entries(SYN).map(([k, s]) => <option key={k} value={k}>{t(s.label)}</option>)}
        </select></label>
        <label>{t('fc.horizon')} <select value={horizon} onChange={e => setHorizon(Number(e.target.value))}>{[30, 60, 120, 360].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <label>{t('fc.step')} <select value={step} onChange={e => setStep(Number(e.target.value))}>{[1, 5, 15].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <button className="btn primary" onClick={run} disabled={busy || ai === false}>{t('fc.run')}</button>
        <span className="tag" role="status">{t('fc.ai')}: {ai === null ? '–' : ai ? `● ${t('fc.on')}` : `■ ${t('fc.off')}`}</span>
      </div>
      {isAdmin && ai !== null && (
        <div className="row" style={{ marginBottom: 12, gap: 8 }}>
          <input aria-label={t('fc.reason')} placeholder={t('fc.reason')} value={reason} onChange={e => setReason(e.target.value)} style={{ width: 260 }} />
          <button className="btn" onClick={toggle} disabled={reason.trim().length < 3}>{ai ? t('fc.switch.off') : t('fc.switch.on')}</button>
        </div>)}
      {ai === false && <div className="empty">{t('fc.disabled')}</div>}
      {err && ai !== false && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && ai !== false && <div className="empty">{t('fc.empty')}</div>}
      {res && b && (<>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('fc.model')}</div><div className="v">{t(`fc.model.${res.model}` as Key)}</div><div className="s">{res.seasonality === 'daily' ? t('fc.seasonal') : t('fc.noseason')}</div></div>
          <div className="card kpi"><div className="l">{t('fc.mae')}</div><div className="v num">{n1(b.mae[res.model], 2)}</div><div className="s num">{b.n_origins} {t('fc.n')}</div></div>
          <div className="card kpi"><div className="l">{t('fc.skill')}</div><div className="v num">{n1(100 * b.skill_vs_persistence[res.model], 0)} %</div></div>
          <div className="card kpi"><div className="l">{t('fc.cov')}</div><div className="v num">{n1(100 * b.coverage['80'], 0)} / {n1(100 * b.coverage['95'], 0)} %</div><div className="s num">{t('fc.nominal')} 80 / 95 %</div></div>
        </div>
        <div className="card" style={{ marginBottom: 12 }}>
          <Chart r={res} />
          <div className="muted" style={{ fontSize: 12 }}>━ {t('fc.history')} · ╌ {t('fc.median')} · ▒ {t('fc.band80')} · ░ {t('fc.band95')}</div>
        </div>
        {res.warnings.map(w => <div key={w} className="muted">▲ {w}</div>)}
        <div className="tablewrap" style={{ maxHeight: 200 }}>
          <table><thead><tr><th>{t('fc.time')}</th><th>{t('fc.low')}</th><th>{t('fc.median')}</th><th>{t('fc.high')}</th></tr></thead>
            <tbody>{res.points.filter((_, i) => i % Math.max(1, Math.floor(res.points.length / 8)) === 0).map(p => (
              <tr key={p.t}><td className="num">{clock(p.t)}</td><td className="num">{n1(p.lo95, 1)}</td><td className="num"><strong>{n1(p.p50, 1)}</strong></td><td className="num">{n1(p.hi95, 1)}</td></tr>))}</tbody></table>
        </div>
      </>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('fc.note')}</p>
    </div>
  );
}
