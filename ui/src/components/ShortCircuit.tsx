import { useEffect, useMemo, useState } from 'react';
import { post } from '../lib/api';
import { useLive } from '../lib/store';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Row { bus: string; kv: number; ik3_ka: number; ik_lg_ka: number; ik_ll_ka: number; ik_llg_ka: number; ip_ka: number; sk_mva: number; x_over_r: number; max_ka: number; rating_ka: number; duty_pct: number }
interface Res { n_buses: number; seconds: number; n_over_rating: number; results: Row[]; c: number }
interface Detail { bus: string; kind: string; fault_current_ka: number; buses: { bus: string; va: number; vb: number; vc: number }[]; branches: { branch: string; ka: number }[] }
type Kind = 'max' | '3ph' | 'lg' | 'll' | 'llg';

const val = (r: Row, k: Kind) => k === '3ph' ? r.ik3_ka : k === 'lg' ? r.ik_lg_ka : k === 'll' ? r.ik_ll_ka : k === 'llg' ? r.ik_llg_ka : r.max_ka;

export function ShortCircuitView({ model }: { model: string }) {
  const live = useLive();
  const [c, setC] = useState(1.1);
  const [kind, setKind] = useState<Kind>('max');
  const [res, setRes] = useState<Res | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [det, setDet] = useState<Detail | null>(null);
  // breakers the SCADA currently reports open (state 1) become the switching overlay for the study
  const open = useMemo(() => [...live.pts.entries()].filter(([k, p]) => k.endsWith('.STATE') && p.s === 1).map(([k]) => k.replace('.STATE', '')).sort(), [live.pts, live.version]);
  const key = open.join(',');
  useEffect(() => {
    let alive = true;
    setBusy(true);
    post<Res>('/ems/v1/short-circuit', { model, open_switches: open, c, top: 400 })
      .then(r => { if (alive) { setRes(r); setErr(null); } })
      .catch(e => { if (alive) setErr((e as Error).message); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, c, key]);
  useEffect(() => {
    if (!sel) { setDet(null); return; }
    let alive = true;
    const row = res?.results.find(r => r.bus === sel);
    const worst: Exclude<Kind, 'max'> = row ? (['3ph', 'lg', 'll', 'llg'] as const).reduce((a, b) => val(row, b) > val(row, a) ? b : a) : '3ph';
    post<Detail>('/ems/v1/short-circuit/detail', { model, open_switches: open, c, bus: sel, kind: kind === 'max' ? worst : kind })
      .then(d => { if (alive) setDet(d); }).catch(() => { if (alive) setDet(null); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel, kind, c, model, key]);

  if (!res) return <div className="empty">{err ? `${t('common.error')}: ${err}` : t('sc.running')}</div>;
  const rows = [...res.results].sort((a, b) => val(b, kind) - val(a, kind));
  const top = rows[0];
  return (
    <div>
      <h1>{t('sc.title')} {busy && <span className="muted">{t('sc.running')}</span>}</h1>
      <div className="grid g4" style={{ marginBottom: 12 }}>
        <div className="card kpi"><div className="l">{t('sc.max')}</div><div className="v num">{n1(top ? val(top, kind) : 0, 1)} kA</div><div className="s">{top?.bus}</div></div>
        <div className="card kpi"><div className="l">{t('sc.over')}</div><div className="v num">{res.n_over_rating}</div><div className="s">{open.length} open breakers in study</div></div>
        <div className="card kpi"><div className="l">{t('sc.buses')}</div><div className="v num">{res.n_buses}</div></div>
        <div className="card kpi"><div className="l">{t('sc.time')}</div><div className="v num">{n1(res.seconds * 1000, 0)} ms</div></div>
      </div>
      <div className="row" style={{ marginBottom: 8, gap: 16, flexWrap: 'wrap' }}>
        <label>{t('sc.c')} <select value={c} onChange={e => setC(Number(e.target.value))}><option value={1.1}>{t('sc.cmax')}</option><option value={0.95}>{t('sc.cmin')}</option></select></label>
        <label>{t('sc.type')} <select value={kind} onChange={e => setKind(e.target.value as Kind)}>
          <option value="max">{t('sc.tmax')}</option><option value="3ph">{t('sc.t3')}</option><option value="lg">{t('sc.tlg')}</option><option value="ll">{t('sc.tll')}</option><option value="llg">{t('sc.tllg')}</option></select></label>
      </div>
      <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,2fr) minmax(0,1fr)' }}>
        <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 380px)' }}>
          <table>
            <thead><tr><th>{t('sc.bus')}</th><th className="num">{t('sc.kv')}</th><th className="num">{t('sc.ik')}</th><th className="num">{t('sc.ip')}</th><th className="num">{t('sc.mva')}</th><th className="num">{t('sc.xr')}</th><th>{t('sc.duty')}</th></tr></thead>
            <tbody>{rows.slice(0, 120).map(r => {
              const duty = val(r, kind) / r.rating_ka * 100;
              return (
                <tr key={r.bus} tabIndex={0} aria-selected={sel === r.bus} onClick={() => setSel(r.bus)} onKeyDown={e => { if (e.key === 'Enter') setSel(r.bus); }} style={{ cursor: 'pointer' }}>
                  <td><strong>{r.bus.replace('Busbar ', 'B')}</strong></td><td className="num">{n1(r.kv, 0)}</td>
                  <td className="num">{n1(val(r, kind), 2)}</td><td className="num">{n1(r.ip_ka, 2)}</td><td className="num">{n1(r.sk_mva, 0)}</td><td className="num">{n1(r.x_over_r, 1)}</td>
                  <td style={{ width: 150 }}><div className={`bar ${duty > 100 ? 'bad' : duty > 80 ? 'warn' : ''}`} title={`${n1(duty, 0)} % of ${r.rating_ka} kA`}><i style={{ width: `${Math.min(100, duty)}%` }} /></div></td>
                </tr>);
            })}</tbody>
          </table>
        </div>
        <div className="card">
          <h2>{t('sc.detail')}</h2>
          {!det ? <div className="muted">{t('sc.select')}</div> : (<>
            <div className="row"><strong>{det.bus.replace('Busbar ', 'B')}</strong><span className="num">{n1(det.fault_current_ka, 2)} kA</span></div>
            <h3 className="muted" style={{ fontSize: 12, margin: '10px 0 4px' }}>{t('sc.lowv')}</h3>
            <table><tbody>{det.buses.slice(0, 6).map(b => <tr key={b.bus}><td>{b.bus.replace('Busbar ', 'B')}</td><td className="num">{n1(Math.min(b.va, b.vb, b.vc), 2)} pu</td></tr>)}</tbody></table>
            <h3 className="muted" style={{ fontSize: 12, margin: '10px 0 4px' }}>{t('sc.branches')}</h3>
            <table><tbody>{det.branches.slice(0, 6).map(b => <tr key={b.branch}><td className="mono">{b.branch}</td><td className="num">{n1(b.ka, 2)} kA</td></tr>)}</tbody></table>
          </>)}
        </div>
      </div>
      <p className="muted" style={{ fontSize: 12 }}>{t('sc.note')}</p>
    </div>
  );
}
