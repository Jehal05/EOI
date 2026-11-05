import { useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';
import { Lines } from './Charts';

interface Area { area: number; buses: number; units: number; load_mw: number; beta_mw_per_hz: number; nadir_hz: number; nadir_time_s: number; final_dev_hz: number; max_abs_ace_mw: number; final_ace_mw: number; final_tie_dev_mw: number; ace_settle_s: number | null }
interface Sim { areas: Area[]; units_at_limit: number; reference: { primary_only_steady_state_hz: number }; series: { t: number[]; f: number[][]; ace: number[][]; tie: number[][] } }
interface Res { agc: Sim; primary_only?: Sim; units: string[] }
export function AgcView({ model }: { model: string }) {
  const [parts, setParts] = useState(2);
  const [kind, setKind] = useState<'load_step' | 'gen_trip'>('load_step');
  const [area, setArea] = useState(0);
  const [mw, setMw] = useState(60);
  const [dur, setDur] = useState(300);
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const allowed = atLeast(getSession(), 'viewer');
  const run = () => {
    setBusy(true); setErr(null);
    const events = [kind === 'load_step' ? { kind, t: 5, area, mw } : { kind, t: 5, area }];
    post<Res>('/ems/v1/agc', { model, parts, events, duration: dur, compare: true })
      .then(setRes).catch(e => { setRes(null); setErr((e as Error).message); }).finally(() => setBusy(false));
  };
  const a = res?.agc, p = res?.primary_only;
  const nA = a?.areas.length ?? 0;
  return (
    <div>
      <h1>{t('agc.title')} {busy && <span className="muted">{t('agc.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 16, flexWrap: 'wrap' }}>
        <label>{t('agc.areas')} <select value={parts} onChange={e => { setParts(Number(e.target.value)); setArea(0); }}>{[2, 3, 4].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <label>{t('agc.event')} <select value={kind} onChange={e => setKind(e.target.value as 'load_step' | 'gen_trip')}><option value="load_step">{t('agc.ev.load')}</option><option value="gen_trip">{t('agc.ev.trip')}</option></select></label>
        <label>{t('agc.area')} <select value={area} onChange={e => setArea(Number(e.target.value))}>{Array.from({ length: parts }, (_, i) => <option key={i} value={i}>{String.fromCharCode(65 + i)}</option>)}</select></label>
        {kind === 'load_step' && <label>{t('agc.mw')} <input type="number" min={1} max={2000} value={mw} onChange={e => setMw(Number(e.target.value))} style={{ width: 80 }} /></label>}
        <label>{t('agc.dur')} <select value={dur} onChange={e => setDur(Number(e.target.value))}>{[120, 300, 600].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <button className="btn primary" onClick={run} disabled={busy || !allowed}>{t('agc.run')}</button>
      </div>
      {err && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && <div className="empty">{t('agc.empty')}</div>}
      {a && (<>
        <div className="grid g2" style={{ marginBottom: 12 }}>
          <div className="card"><Lines title={t('agc.freq')} ts={a.series.t} unit="Hz" series={[
            ...a.series.f[0].map((_, i) => ({ name: `${String.fromCharCode(65 + i)} ${t('agc.with')}`, i, y: a.series.f.map(r => r[i]) })),
            ...(p ? p.series.f[0].map((_, i) => ({ name: `${String.fromCharCode(65 + i)} ${t('agc.without')}`, i, dim: true, y: p.series.f.map(r => r[i]) })) : [])]} />
            <div className="muted" style={{ fontSize: 12 }}>{t('agc.with')}: {t('agc.chart')} ━ / {t('agc.without')}: ┄</div></div>
          <div className="card"><Lines title={t('agc.ace')} ts={a.series.t} unit="MW" series={a.series.ace[0].map((_, i) => ({ name: `${String.fromCharCode(65 + i)}`, i, y: a.series.ace.map(r => r[i]) }))} /></div>
        </div>
        <div className="tablewrap" style={{ maxHeight: 260 }}>
          <table>
            <thead><tr><th>{t('agc.area')}</th><th>{t('agc.units')}</th><th>{t('agc.bias')}</th><th>{t('agc.nadir')}</th><th>{t('agc.final')} (Hz)</th><th>{t('agc.tie')} (MW)</th><th>{t('agc.maxace')}</th><th>{t('agc.finalace')}</th><th>{t('agc.settle')}</th></tr></thead>
            <tbody>{a.areas.map((r, i) => (
              <tr key={r.area}>
                <td><strong>{String.fromCharCode(65 + r.area)}</strong></td><td className="num">{r.units}</td><td className="num">{n1(r.beta_mw_per_hz, 0)}</td>
                <td className="num">{n1(r.nadir_hz, 3)} @ {n1(r.nadir_time_s, 0)} s{p ? ` (${n1(p.areas[i].nadir_hz, 3)})` : ''}</td>
                <td className="num">{n1(r.final_dev_hz, 3)}{p ? ` (${n1(p.areas[i].final_dev_hz, 3)})` : ''}</td>
                <td className="num">{n1(r.final_tie_dev_mw, 1)}</td><td className="num">{n1(r.max_abs_ace_mw, 1)}</td><td className="num">{n1(r.final_ace_mw, 1)}</td>
                <td className="num">{r.ace_settle_s === null ? t('agc.never') : `${n1(r.ace_settle_s, 0)} s`}</td>
              </tr>))}</tbody>
          </table>
        </div>
        <p className="muted num" style={{ fontSize: 12 }}>{t('agc.ref')}: {n1(a.reference.primary_only_steady_state_hz, 3)} Hz · {t('agc.limit')}: {a.units_at_limit} · {nA} {t('agc.areas')}</p>
      </>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('agc.note')}</p>
    </div>
  );
}
