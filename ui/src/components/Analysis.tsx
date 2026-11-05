import { Fragment, useState } from 'react';
import { usePoll, useNow } from '../lib/hooks';
import { useLive } from '../lib/store';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';
import { DistributedCard } from './Distributed';

interface CaseRes { outage: string; from_bus: string; to_bus: string; converged: boolean; islanded: boolean; lost_load_mw: number; overloads: { branch: string; loading_pct: number }[]; voltage_violations: { bus: string; vm: number }[]; max_loading_pct: number; min_vm: number; max_vm: number; severity: number }
interface N2Res { outage: string; branches: string[]; dc_severity: number; dc_max_loading_pct: number | null; dc_islanding: boolean; ac_verified: boolean; converged?: boolean; islanded?: boolean; lost_load_mw?: number; overloads?: { branch: string; loading_pct: number }[]; voltage_violations?: { bus: string; vm: number }[]; max_loading_pct?: number | null; severity?: number }
interface N2 { error?: string; n_pairs_total: number; n_candidates: number; n_ac_verified: number; n_islanding_candidates: number; dc_screen_seconds: number; wall_seconds: number; n_with_violations: number; results: N2Res[] }
interface Ems {
  ts?: number; error?: string; cycle_ms?: number; se_roundtrip_ms?: number; measurements_used?: number; open_breakers?: number;
  se?: { observable?: boolean; converged?: boolean; J?: number; dof?: number; chi2_threshold?: number; solve_ms?: number; n_measurements?: number; removed?: { id: string; kind: string; value: number; estimate: number; normalized_residual: number }[]; top_residuals?: { id: string; kind: string; residual: number; normalized: number }[] };
  contingency_n2?: N2;
  contingency?: { wall_seconds: number; mean_case_ms: number; workers: number; n_with_violations: number; n_nonconverged: number; base: { losses_mw: number; violations: { overloads: number; voltage: number } }; results: CaseRes[] };
}

function Age({ ts }: { ts?: number }) {
  const now = useNow(1000);
  return <span className="muted">{ts ? `${t('common.age')} ${Math.max(0, Math.round((now - ts) / 1000))} ${t('common.seconds')}` : ''}</span>;
}

export function ContingencyView() {
  const { data, loading } = usePoll<Ems>('/api/ems', 4000);
  const [open, setOpen] = useState<string | null>(null);
  const [mode, setMode] = useState<'n1' | 'n2'>('n1');
  const c = data?.contingency;
  if (loading && !data) return <div className="empty"><div className="skeleton" style={{ width: 240, margin: '0 auto' }} /></div>;
  if (!c) return <div className="empty">{data?.error ? `${t('common.error')}: ${data.error}` : t('cont.waiting')}</div>;
  const max = Math.max(...c.results.map(r => r.severity), 1);
  return (
    <div>
      <h1>{t('cont.title')} <Age ts={data?.ts} /></h1>
      <DistributedCard model="ieee118" />
      <div className="row" role="tablist" style={{ marginBottom: 8 }}>
        {(['n1', 'n2'] as const).map(m => <button key={m} role="tab" aria-selected={mode === m} className={`btn ${mode === m ? 'primary' : ''}`} onClick={() => setMode(m)}>{t(`cont.${m}`)}</button>)}
      </div>
      {mode === 'n2' ? <N2Board n2={data?.contingency_n2} /> : (<>
      <div className="grid g4" style={{ marginBottom: 12 }}>
        <div className="card kpi"><div className="l">{t('cont.cases')}</div><div className="v num">{(c as unknown as { cases: number }).cases}</div><div className="s">{c.workers} workers</div></div>
        <div className="card kpi"><div className="l">{t('cont.viol')}</div><div className="v num">{c.n_with_violations}</div><div className="s">{c.n_nonconverged} non-converged</div></div>
        <div className="card kpi"><div className="l">{t('cont.wall')}</div><div className="v num">{n1(c.wall_seconds, 2)} s</div><div className="s">{t('cont.mean')}: {n1(c.mean_case_ms)} ms</div></div>
        <div className="card kpi"><div className="l">{t('cont.base')}</div><div className="v num">{n1(c.base.losses_mw)} MW</div><div className="s">{c.base.violations.voltage} V · {c.base.violations.overloads} OL</div></div>
      </div>
      <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 360px)' }}>
        <table>
          <thead><tr><th>{t('cont.outage')}</th><th className="num">{t('cont.maxload')}</th><th className="num">{t('cont.overloads')}</th><th className="num">{t('cont.voltage')}</th><th>{t('cont.islanded')}</th><th>{t('cont.severity')}</th></tr></thead>
          <tbody>{c.results.map(r => (
            <Fragment key={r.outage}>
              <tr onClick={() => setOpen(open === r.outage ? null : r.outage)} style={{ cursor: 'pointer' }} aria-expanded={open === r.outage}>
                <td><strong>{r.outage}</strong> <span className="muted">{r.from_bus.replace('Busbar ', 'B')} – {r.to_bus.replace('Busbar ', 'B')}</span></td>
                <td className="num warn">{n1(r.max_loading_pct, 0)} %</td><td className="num">{r.overloads.length}</td><td className="num">{r.voltage_violations.length}</td>
                <td>{r.islanded ? `${t('common.yes')}${r.lost_load_mw ? ` · ${t('cont.lost')} ${n1(r.lost_load_mw)} MW` : ''}` : t('common.no')}</td>
                <td style={{ width: 160 }}><div className={`bar ${r.severity / max > 0.6 ? 'bad' : 'warn'}`}><i style={{ width: `${(r.severity / max) * 100}%` }} /></div></td>
              </tr>
              {open === r.outage && <tr><td colSpan={6} className="muted" style={{ background: 'var(--panel2)' }}>
                {r.overloads.slice(0, 12).map(o => <span key={o.branch} className="tag" style={{ marginRight: 6 }}>{o.branch} {n1(o.loading_pct, 0)} %</span>)}
                {r.voltage_violations.map(o => <span key={o.bus} className="tag" style={{ marginRight: 6 }}>{o.bus} {n1(o.vm, 3)} pu</span>)}
              </td></tr>}
            </Fragment>))}</tbody>
        </table>
      </div>
      </>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('cont.note')}</p>
    </div>
  );
}

function N2Board({ n2 }: { n2?: N2 }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!n2 || n2.error) return <div className="empty">{n2?.error ? `${t('common.error')}: ${n2.error}` : t('cont.waiting')}</div>;
  const ver = n2.results.filter(r => r.ac_verified);
  const max = Math.max(...ver.map(r => r.severity ?? 0), 1);
  return (
    <div>
      <div className="grid g4" style={{ marginBottom: 12 }}>
        <div className="card kpi"><div className="l">{t('cont.n2cand')}</div><div className="v num">{n2.n_candidates}</div><div className="s num">{n2.n_pairs_total} {t('cont.n2space')}</div></div>
        <div className="card kpi"><div className="l">{t('cont.n2ver')}</div><div className="v num">{n2.n_ac_verified}</div><div className="s num">{n2.n_islanding_candidates} islanding</div></div>
        <div className="card kpi"><div className="l">{t('cont.viol')}</div><div className="v num">{n2.n_with_violations}</div></div>
        <div className="card kpi"><div className="l">{t('cont.wall')}</div><div className="v num">{n1(n2.wall_seconds, 2)} s</div><div className="s">DC {n1(n2.dc_screen_seconds * 1000, 0)} ms</div></div>
      </div>
      <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 380px)' }}>
        <table>
          <thead><tr><th>{t('cont.outage')}</th><th className="num">{t('cont.dcmax')}</th><th className="num">{t('cont.maxload')}</th><th className="num">{t('cont.overloads')}</th><th>{t('cont.islanded')}</th><th>{t('cont.severity')}</th></tr></thead>
          <tbody>{n2.results.map(r => (
            <Fragment key={r.outage}>
              <tr onClick={() => setOpen(open === r.outage ? null : r.outage)} style={{ cursor: 'pointer' }} aria-expanded={open === r.outage}>
                <td><strong>{r.branches.join(' + ')}</strong> <span className="tag">{r.ac_verified ? t('cont.acv') : t('cont.dcest')}</span></td>
                <td className="num">{r.dc_islanding ? '—' : `${n1(r.dc_max_loading_pct ?? 0, 0)} %`}</td>
                <td className="num warn">{r.ac_verified && r.max_loading_pct != null ? `${n1(r.max_loading_pct, 0)} %` : '—'}</td>
                <td className="num">{r.ac_verified ? (r.overloads?.length ?? 0) : '—'}</td>
                <td>{(r.ac_verified ? r.islanded : r.dc_islanding) ? `${t('common.yes')}${r.lost_load_mw ? ` · ${t('cont.lost')} ${n1(r.lost_load_mw)} MW` : ''}` : t('common.no')}</td>
                <td style={{ width: 160 }}>{r.ac_verified ? <div className={`bar ${(r.severity ?? 0) / max > 0.6 ? 'bad' : 'warn'}`}><i style={{ width: `${((r.severity ?? 0) / max) * 100}%` }} /></div> : <span className="muted">—</span>}</td>
              </tr>
              {open === r.outage && r.ac_verified && <tr><td colSpan={6} className="muted" style={{ background: 'var(--panel2)' }}>
                {(r.overloads ?? []).slice(0, 12).map(o => <span key={o.branch} className="tag" style={{ marginRight: 6 }}>{o.branch} {n1(o.loading_pct, 0)} %</span>)}
                {(r.voltage_violations ?? []).map(o => <span key={o.bus} className="tag" style={{ marginRight: 6 }}>{o.bus} {n1(o.vm, 3)} pu</span>)}
              </td></tr>}
            </Fragment>))}</tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12 }}>{t('cont.n2note')}</p>
    </div>
  );
}

export function SeView() {
  const { data, loading } = usePoll<Ems>('/api/ems', 4000);
  const live = useLive();
  const se = data?.se;
  if (loading && !data) return <div className="empty"><div className="skeleton" style={{ width: 240, margin: '0 auto' }} /></div>;
  if (!se) return <div className="empty">{data?.error ? `${t('common.error')}: ${data.error}` : t('cont.waiting')}</div>;
  const J = se.J ?? 0, th = se.chi2_threshold ?? 1, good = [...live.pts.values()].filter(p => !p.q).length, tot = Math.max(live.pts.size, 1);
  return (
    <div>
      <h1>{t('se.title')} <Age ts={data?.ts} /></h1>
      <div className="grid g4" style={{ marginBottom: 12 }}>
        <div className="card kpi"><div className="l">{t('se.observable')}</div><div className="v">{se.observable ? t('common.yes') : t('common.no')}</div><div className="s">{se.converged ? 'converged' : '—'}</div></div>
        <div className="card kpi"><div className="l">{t('se.meas')}</div><div className="v num">{data?.measurements_used}</div><div className="s">{t('se.dof')}: {se.dof}</div></div>
        <div className="card kpi"><div className="l">{t('se.time')}</div><div className="v num">{n1(se.solve_ms, 0)} ms</div><div className="s">{t('se.cycle')}: {n1(data?.cycle_ms, 0)} ms</div></div>
        <div className="card kpi"><div className="l">{t('se.good')}</div><div className="v num">{n1((good / tot) * 100, 1)} %</div><div className="s num">{good} / {tot}</div></div>
      </div>
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>{t('se.j')}</h2>
        <div className="row"><span className="num" style={{ fontSize: 22 }}>{n1(J, 1)}</span><span className="muted">/ {t('se.threshold')} {n1(th, 1)}</span></div>
        <div className={`bar ${J > th ? 'bad' : J > 0.9 * th ? 'warn' : ''}`} style={{ marginTop: 6 }} role="meter" aria-valuenow={J} aria-valuemax={th}><i style={{ width: `${Math.min(100, (J / th) * 100)}%` }} /></div>
      </div>
      <div className="grid g2">
        <div className="card"><h2>{t('se.removed')}</h2>
          {(se.removed?.length ?? 0) === 0 ? <div className="muted">{t('se.none')}</div> : (
            <table><thead><tr><th>ID</th><th className="num">{t('se.measured')}</th><th className="num">{t('se.estimated')}</th><th className="num">r<sub>N</sub></th></tr></thead>
              <tbody>{se.removed!.map(r => <tr key={r.id}><td className="mono bad">{r.id}</td><td className="num">{n1(r.value, 2)}</td><td className="num">{n1(r.estimate, 2)}</td><td className="num">{n1(r.normalized_residual, 1)}</td></tr>)}</tbody></table>)}
        </div>
        <div className="card"><h2>{t('se.residuals')}</h2>
          <table><thead><tr><th>ID</th><th className="num">{t('se.residual')}</th><th className="num">r<sub>N</sub></th></tr></thead>
            <tbody>{(se.top_residuals ?? []).slice(0, 8).map(r => <tr key={r.id}><td className="mono">{r.id}</td><td className="num">{n1(r.residual, 3)}</td><td className="num">{n1(r.normalized, 2)}</td></tr>)}</tbody></table>
        </div>
      </div>
    </div>
  );
}
