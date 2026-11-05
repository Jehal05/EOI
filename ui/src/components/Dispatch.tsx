import { useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { useLive } from '../lib/store';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Gen { id: string; bus: string; p_mw: number; pmin: number; pmax: number; p_ref_mw: number }
interface Cong { branch: string; flow_mw: number; limit_mw: number; loading_pct: number; shadow_price: number }
interface N1 { cases: number; cases_over_emergency: number; worst_loading_pct: number }
interface Res {
  kind: 'opf' | 'scopf'; cost_per_h: number; reference_cost_per_h: number; unserved_mw: number; system_lambda: number; lmp_min: number; lmp_max: number;
  generators: Gen[]; lmp: { bus: string; lmp: number }[]; congested: Cong[]; n_congested: number; shed: { bus: string; mw: number }[]; wall_seconds: number;
  ac?: { converged: boolean; losses_mw: number; n_overloads: number; n_voltage_violations: number; min_vm: number; max_vm: number } | null;
  security_cost_per_h?: number; n_cuts?: number; iterations?: number; ac_n1?: { secured?: N1; unsecured?: N1 } | null; n_islanding_not_secured?: number;
}

export function DispatchView({ model }: { model: string }) {
  const live = useLive();
  const [mode, setMode] = useState<'opf' | 'scopf'>('opf');
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const open = [...live.pts.entries()].filter(([k, p]) => k.endsWith('.STATE') && p.s === 1).map(([k]) => k.replace('.STATE', '')).sort();
  const allowed = atLeast(getSession(), 'operator');
  const run = () => {
    setBusy(true); setErr(null);
    post<Res>('/ems/v1/opf', { model, open_switches: open, mode, vmin: 0.93, vmax: 1.07 })
      .then(setRes).catch(e => setErr((e as Error).message)).finally(() => setBusy(false));
  };
  const saving = res ? res.reference_cost_per_h - res.cost_per_h : 0;
  const topLmp = res ? [...res.lmp].sort((a, b) => b.lmp - a.lmp).slice(0, 8) : [];
  return (
    <div>
      <h1>{t('op.title')} {busy && <span className="muted">{t('op.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <label>{t('op.mode')} <select value={mode} onChange={e => setMode(e.target.value as 'opf' | 'scopf')}><option value="opf">{t('op.opf')}</option><option value="scopf">{t('op.scopf')}</option></select></label>
        <button className="btn primary" onClick={run} disabled={busy || !allowed}>{t('op.run')}</button>
      </div>
      {err && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && <div className="empty">{t('op.empty')}</div>}
      {res && (<>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('op.cost')}</div><div className="v num">{n1(res.cost_per_h, 0)} /h</div><div className="s">{n1(res.wall_seconds, 2)} s</div></div>
          <div className="card kpi"><div className="l">{t('op.save')}</div><div className="v num">{n1(saving, 0)} /h</div><div className="s num">{n1((saving / Math.max(res.reference_cost_per_h, 1)) * 100, 2)} %</div></div>
          <div className="card kpi"><div className="l">{t('op.lambda')}</div><div className="v num">{n1(res.system_lambda, 2)}</div><div className="s num">{n1(res.lmp_min, 1)} – {n1(res.lmp_max, 1)} /MWh</div></div>
          <div className="card kpi"><div className="l">{res.kind === 'scopf' ? t('op.sec') : t('op.unserved')}</div>
            <div className="v num">{res.kind === 'scopf' ? `${n1(res.security_cost_per_h ?? 0, 0)} /h` : `${n1(res.unserved_mw, 1)} MW`}</div>
            <div className="s num">{res.kind === 'scopf' ? `${res.n_cuts} ${t('op.cuts')} · ${t('op.unserved')} ${n1(res.unserved_mw, 1)} MW` : `${res.n_congested} ${t('op.cong')}`}</div></div>
        </div>
        <div className="grid g2" style={{ marginBottom: 12 }}>
          <div className="card"><h2>{t('op.ac')}</h2>
            {res.ac ? (<>
              <div>{t('op.acok')}: <strong>{res.ac.converged ? t('common.yes') : t('common.no')}</strong> · {n1(res.ac.losses_mw, 1)} MW losses</div>
              <div className="muted num">{res.ac.n_overloads} OL · {res.ac.n_voltage_violations} V · {n1(res.ac.min_vm, 3)}–{n1(res.ac.max_vm, 3)} pu</div>
              {res.ac_n1 && <div style={{ marginTop: 6 }}>{t('op.acn1')}: {res.ac_n1.secured && <span className="tag">{t('op.secured')} {res.ac_n1.secured.cases_over_emergency} / {res.ac_n1.secured.cases}</span>} {res.ac_n1.unsecured && <span className="tag">{t('op.unsecured')} {res.ac_n1.unsecured.cases_over_emergency} / {res.ac_n1.unsecured.cases}</span>}</div>}
            </>) : <div className="muted">—</div>}
            {res.shed.length > 0 && <div style={{ marginTop: 6 }} className="warn">{t('op.shed')}: {res.shed.map(s => `${s.bus.replace('Busbar ', 'B')} ${n1(s.mw, 1)}`).join(', ')} MW</div>}
          </div>
          <div className="card"><h2>{t('op.cong')}</h2>
            {res.congested.length === 0 ? <div className="muted">—</div> : (
              <table><thead><tr><th>ID</th><th className="num">{t('op.flow')}</th><th className="num">{t('op.limit')}</th><th className="num">{t('op.shadow')}</th></tr></thead>
                <tbody>{res.congested.slice(0, 6).map(c => <tr key={c.branch}><td className="mono">{c.branch}</td><td className="num">{n1(c.flow_mw, 1)}</td><td className="num">{n1(c.limit_mw, 0)}</td><td className="num">{n1(c.shadow_price, 2)}</td></tr>)}</tbody></table>)}
          </div>
        </div>
        <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,2fr) minmax(0,1fr)' }}>
          <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 520px)', minHeight: 180 }}>
            <table>
              <thead><tr><th>{t('op.gen')}</th><th className="num">{t('op.p')}</th><th className="num">{t('op.pref')}</th><th className="num">{t('op.delta')}</th><th>Pmax</th></tr></thead>
              <tbody>{[...res.generators].filter(g => g.pmax > 0).sort((a, b) => Math.abs(b.p_mw - b.p_ref_mw) - Math.abs(a.p_mw - a.p_ref_mw)).map(g => (
                <tr key={g.id}><td><strong>{g.id}</strong> <span className="muted">{g.bus.replace('Busbar ', 'B')}</span></td><td className="num">{n1(g.p_mw, 1)}</td><td className="num">{n1(g.p_ref_mw, 1)}</td>
                  <td className="num">{g.p_mw - g.p_ref_mw >= 0 ? '▲' : '▼'} {n1(Math.abs(g.p_mw - g.p_ref_mw), 1)}</td>
                  <td style={{ width: 110 }}><div className={`bar ${g.p_mw >= g.pmax - 0.01 ? 'warn' : ''}`}><i style={{ width: `${Math.min(100, (g.p_mw / Math.max(g.pmax, 1)) * 100)}%` }} /></div></td></tr>))}</tbody>
            </table>
          </div>
          <div className="card"><h2>{t('op.lmp')}</h2>
            <table><tbody>{topLmp.map(l => <tr key={l.bus}><td>{l.bus.replace('Busbar ', 'B')}</td><td className="num">{n1(l.lmp, 2)} /MWh</td></tr>)}</tbody></table>
          </div>
        </div>
      </>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('op.note')}</p>
    </div>
  );
}
