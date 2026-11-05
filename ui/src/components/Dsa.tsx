import { useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';
import { Lines } from './Charts';

type Tab = 'voltage' | 'transient' | 'smallsignal';
interface Mode { freq_hz: number; damping_ratio: number | null; participants?: { unit: string; rel: number }[] }
interface Res {
  status: string; wall_seconds: number;
  base?: { margin_pct: number; margin_mw: number; weakest_bus_name: string; pv_curve: { lam: number; vm: number }[]; q_limited_buses: number }; n1?: { outage: string; margin_pct: number; weakest_bus: string }[]; worst_margin_pct?: number;
  n_faults?: number; n_unstable?: number; results?: { bus: string; stable: boolean; max_angle_sep_deg: number; tripped: string | null; cct_s?: number | null; cct_margin_ms?: number | null }[];
  trajectory?: { bus: string; t: number[]; units: string[]; angle_deg: number[][] };
  modes?: Mode[]; min_damping_ratio?: number; n_poorly_damped?: number; n_modes?: number; freq_range_hz?: number[];
}

export function DsaView({ model }: { model: string }) {
  const [tab, setTab] = useState<Tab>('voltage');
  const [clear, setClear] = useState(0.12);
  const [trip, setTrip] = useState(true);
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const allowed = atLeast(getSession(), 'viewer');
  const run = () => {
    setBusy(true); setErr(null); setRes(null);
    const body = tab === 'transient' ? { model, clearing_time: clear, trip_branch: trip } : { model };
    post<Res>(`/ems/v1/dsa/${tab}`, body).then(setRes).catch(e => setErr((e as Error).message)).finally(() => setBusy(false));
  };
  const switchTab = (x: Tab) => { setTab(x); setRes(null); setErr(null); };
  return (
    <div>
      <h1>{t('dsa.title')} {busy && <span className="muted">{t('dsa.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 8, flexWrap: 'wrap' }}>
        <div role="tablist" className="row" style={{ gap: 8 }}>
          {(['voltage', 'transient', 'smallsignal'] as Tab[]).map(x => (
            <button key={x} role="tab" aria-selected={tab === x} className={`btn ${tab === x ? 'primary' : ''}`} onClick={() => switchTab(x)}>{t(x === 'voltage' ? 'dsa.voltage' : x === 'transient' ? 'dsa.transient' : 'dsa.small')}</button>))}
        </div>
        {tab === 'transient' && <>
          <label>{t('dsa.clear')} <select value={clear} onChange={e => setClear(Number(e.target.value))}>{[0.08, 0.12, 0.2, 0.3].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
          <label><input type="checkbox" checked={trip} onChange={e => setTrip(e.target.checked)} /> {t('dsa.trip')}</label></>}
        <button className="btn primary" onClick={run} disabled={busy || !allowed}>{t('dsa.run')}</button>
      </div>
      {err && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && <div className="empty">{t('dsa.empty')}</div>}
      {res && tab === 'voltage' && res.base && (<>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('dsa.margin')}</div><div className="v num">{n1(res.base.margin_pct, 0)} %</div><div className="s num">{n1(res.base.margin_mw, 0)} MW · {t('dsa.margin.sub')}</div></div>
          <div className="card kpi"><div className="l">{t('dsa.weak')}</div><div className="v">{res.base.weakest_bus_name}</div></div>
          <div className="card kpi"><div className="l">{t('dsa.worst')}</div><div className="v num">{n1(res.worst_margin_pct ?? 0, 0)} %</div><div className="s">{res.n1?.[0]?.outage}</div></div>
          <div className="card kpi" role="status"><div className="l">{t('dsa.stable')}</div><div className="v">{res.status === 'ok' ? `● ${t('dsa.status.ok2')}` : `■ ${t('dsa.status.low_margin')}`}</div></div>
        </div>
        <div className="grid g2">
          <div className="card"><Lines title={t('dsa.pv')} ts={res.base.pv_curve.map(p => p.lam * 100)} xUnit="%" digits={2} unit="pu" series={[{ name: res.base.weakest_bus_name, i: 0, y: res.base.pv_curve.map(p => p.vm) }]} /></div>
          <div className="tablewrap" style={{ maxHeight: 240 }}><table><thead><tr><th>{t('dsa.outage')}</th><th>{t('dsa.margin')} %</th><th>{t('dsa.weak')}</th></tr></thead>
            <tbody>{(res.n1 ?? []).map(r => <tr key={r.outage}><td><strong>{r.outage}</strong></td><td className="num">{n1(r.margin_pct, 0)}</td><td>{r.weakest_bus}</td></tr>)}</tbody></table></div>
        </div>
      </>)}
      {res && tab === 'transient' && res.results && (<>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('dsa.faults')}</div><div className="v num">{res.n_faults}</div></div>
          <div className="card kpi" role="status"><div className="l">{t('dsa.unstable')}</div><div className="v num">{(res.n_unstable ?? 0) > 0 ? '■ ' : '● '}{res.n_unstable}</div></div>
          <div className="card kpi"><div className="l">{t('dsa.cct')}</div><div className="v num">{n1(Math.min(...res.results.filter(r => r.cct_s != null).map(r => r.cct_s as number)), 2)}</div></div>
        </div>
        <div className="grid g2">
          <div className="tablewrap" style={{ maxHeight: 320 }}><table><thead><tr><th>{t('dsa.bus')}</th><th>{t('dsa.stable')}</th><th>{t('dsa.sep')}</th><th>{t('dsa.cct')}</th><th>{t('dsa.cctm')}</th></tr></thead>
            <tbody>{res.results.slice(0, 40).map(r => <tr key={r.bus}><td><strong>{r.bus}</strong></td><td>{r.stable ? `● ${t('dsa.stab.yes')}` : `■ ${t('dsa.stab.no')}`}</td><td className="num">{n1(r.max_angle_sep_deg, 0)}</td>
              <td className="num">{r.cct_s === undefined ? '–' : r.cct_s === null ? t('dsa.cct.none') : n1(r.cct_s, 3)}</td><td className="num">{r.cct_margin_ms == null ? '–' : n1(r.cct_margin_ms, 0)}</td></tr>)}</tbody></table></div>
          {res.trajectory && <div className="card"><Lines title={`${t('dsa.swing')} · ${res.trajectory.bus}`} ts={res.trajectory.t} unit="°" digits={0}
            series={res.trajectory.units.map((u, i) => ({ name: u, i, y: res.trajectory!.angle_deg.map(r => r[i]) }))} /></div>}
        </div>
      </>)}
      {res && tab === 'smallsignal' && res.modes && (<>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('dsa.modes')}</div><div className="v num">{res.n_modes}</div><div className="s num">{res.freq_range_hz ? `${n1(res.freq_range_hz[0], 2)}–${n1(res.freq_range_hz[1], 2)} Hz` : ''}</div></div>
          <div className="card kpi"><div className="l">{t('dsa.mindamp')}</div><div className="v num">{n1(100 * (res.min_damping_ratio ?? 0), 1)} %</div></div>
          <div className="card kpi"><div className="l">{t('dsa.poor')}</div><div className="v num">{res.n_poorly_damped}</div></div>
          <div className="card kpi" role="status"><div className="l">{t('dsa.stable')}</div><div className="v">{res.status === 'ok' ? '● ' : '▲ '}{t(`dsa.status.${res.status}` as Key)}</div></div>
        </div>
        <div className="tablewrap" style={{ maxHeight: 320 }}><table><thead><tr><th>{t('dsa.freq')}</th><th>{t('dsa.damp')}</th><th>{t('dsa.part')}</th></tr></thead>
          <tbody>{res.modes.map((m, i) => <tr key={i}><td className="num">{n1(m.freq_hz, 3)}</td><td className="num">{m.damping_ratio === null ? '–' : `${n1(100 * m.damping_ratio, 1)} %`}</td><td>{m.participants?.map(p => `${p.unit} (${n1(p.rel, 2)})`).join(', ')}</td></tr>)}</tbody></table></div>
      </>)}
      {res && <p className="muted num" style={{ fontSize: 12 }}>{n1(res.wall_seconds, 1)} s</p>}
      <p className="muted" style={{ fontSize: 12 }}>{t('dsa.note')}</p>
    </div>
  );
}
