import { useState } from 'react';
import { post } from '../lib/api';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';
import { Lines } from './Charts';

interface Run { f_nadir: number; f_final: number; shed_mw: number; shed_pct: number; t: number[]; f: number[]; stages: { f: number; frac: number; fired_at: number | null }[] }
interface Scen { name: string; lost_mw: number; lost_pct: number; without: Run; with_ufls: Run | null; passes: boolean }
interface Ufls { scenarios: Scen[]; stages: { f: number; delay: number; frac: number }[]; criteria: { nadir_min: number; final_min: number }; load_mw: number }
interface Per { k: number; start_h: number; loads: string[]; shed_mw: number; shortfall_mw: number; ac?: { converged: boolean; overloads: number; max_loading_pct: number }; ac_retries?: number }
interface Ros { feasible: boolean; periods: Per[]; jain_fairness: number; loads_ever_shed: number; max_ens_mwh: number; eligible_loads: number; ac_base: { overloads: number } | null }
interface Roster { roster: Ros; static?: { loads_ever_shed: number; jain_fairness: number; max_ens_mwh: number } }
interface Prio { classes: Record<string, { name: string; loads: number; mw: number }>; total_mw: number; loads: { load: string; bus: string; mw: number; cls: number }[]; synthetic: boolean }
const fill = (s: string, a: number, b: number) => s.replace('{a}', String(a)).replace('{b}', String(b));

export function ShedView({ model }: { model: string }) {
  const [tab, setTab] = useState<'ufls' | 'roster' | 'prio'>('ufls');
  return (
    <div>
      <h1>{t('sh.title')}</h1>
      <div role="tablist" className="row" style={{ gap: 6, marginBottom: 10 }}>
        {(['ufls', 'roster', 'prio'] as const).map(k => <button key={k} role="tab" aria-selected={tab === k} className={'btn' + (tab === k ? ' primary' : '')} onClick={() => setTab(k)}>{t(`sh.tab.${k}` as Key)}</button>)}
      </div>
      {tab === 'ufls' && <UflsTab model={model} />}
      {tab === 'roster' && <RosterTab model={model} />}
      {tab === 'prio' && <PrioTab model={model} />}
      <p className="muted" style={{ fontSize: 12 }}>{t('sh.footer')}</p>
    </div>
  );
}

function UflsTab({ model }: { model: string }) {
  const [r, setR] = useState<Ufls | null>(null); const [busy, setBusy] = useState(false); const [err, setErr] = useState(''); const [sel, setSel] = useState(0);
  const run = () => { setBusy(true); setErr(''); post<Ufls>('/ems/v1/shed/ufls', { model }).then(x => { setR(x); setSel(x.scenarios.length - 1); }).catch(e => setErr((e as Error).message)).finally(() => setBusy(false)); };
  const s = r?.scenarios[sel];
  return (
    <div>
      <button className="btn primary" onClick={run} disabled={busy}>{t('sh.run')}</button>
      {err && <div role="alert" className="errmsg">{err}</div>}
      {r && <>
        <p className="muted">{fill(t('sh.crit'), r.criteria.nadir_min, r.criteria.final_min)}</p>
        <div className="tablewrap"><table><thead><tr><th>{t('sh.scen')}</th><th>{t('sh.lost')}</th><th>{t('sh.nadir0')}</th><th>{t('sh.nadir1')}</th><th>{t('sh.final1')}</th><th>{t('sh.shed')}</th><th>{t('sh.result')}</th></tr></thead>
          <tbody>{r.scenarios.map((x, i) => (
            <tr key={x.name} aria-selected={i === sel} onClick={() => setSel(i)} style={{ cursor: 'pointer', background: i === sel ? 'var(--row-sel, transparent)' : undefined }}>
              <td><button className="btn" onClick={() => setSel(i)}>{x.name}</button></td>
              <td className="num">{n1(x.lost_mw, 0)} MW ({n1(x.lost_pct, 0)} %)</td><td className="num">{n1(x.without.f_nadir, 2)} Hz</td>
              <td className="num">{x.with_ufls ? n1(x.with_ufls.f_nadir, 2) : '–'} Hz</td><td className="num">{x.with_ufls ? n1(x.with_ufls.f_final, 2) : '–'} Hz</td>
              <td className="num">{x.with_ufls ? `${n1(x.with_ufls.shed_mw, 0)} MW (${n1(x.with_ufls.shed_pct, 0)} %)` : '–'}</td>
              <td><span className="tag">{x.passes ? '● ' + t('sh.pass') : '■ ' + t('sh.fail')}</span></td></tr>))}</tbody></table></div>
        {s && s.with_ufls && <div className="card" style={{ marginTop: 10 }}>
          <Lines title={`${t('sh.freq')} — ${s.name}`} ts={s.without.t} unit="Hz" series={[{ name: t('sh.without'), i: 1, y: s.without.f, dim: true }, { name: t('sh.with'), i: 0, y: s.with_ufls.f }]} />
          <h3>{t('sh.stages')}</h3>
          <table><thead><tr><th>{t('sh.pick')}</th><th>{t('sh.frac')}</th><th>{t('sh.fired')}</th></tr></thead>
            <tbody>{s.with_ufls.stages.map(st => <tr key={st.f}><td className="num">{st.f.toFixed(1)}</td><td className="num">{(st.frac * 100).toFixed(0)} %</td><td className="num">{st.fired_at === null ? t('sh.never') : st.fired_at.toFixed(2)}</td></tr>)}</tbody></table>
        </div>}
      </>}
    </div>
  );
}

function RosterTab({ model }: { model: string }) {
  const [deficit, setDeficit] = useState(150); const [hours, setHours] = useState(12); const [period, setPeriod] = useState(1); const [minc, setMinc] = useState(3);
  const [r, setR] = useState<Roster | null>(null); const [busy, setBusy] = useState(false); const [err, setErr] = useState('');
  const run = () => { setBusy(true); setErr(''); post<Roster>('/ems/v1/shed/roster', { model, deficit_mw: deficit, hours, period_h: period, min_class: minc, compare: true }).then(setR).catch(e => setErr((e as Error).message)).finally(() => setBusy(false)); };
  return (
    <div>
      <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label>{t('sh.deficit')} <input type="number" min={1} value={deficit} onChange={e => setDeficit(Number(e.target.value))} style={{ width: 90 }} /></label>
        <label>{t('sh.hours')} <input type="number" min={1} max={72} value={hours} onChange={e => setHours(Number(e.target.value))} style={{ width: 70 }} /></label>
        <label>{t('sh.period')} <select value={period} onChange={e => setPeriod(Number(e.target.value))}>{[0.5, 1, 2, 3].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <label>{t('sh.mincls')} <select value={minc} onChange={e => setMinc(Number(e.target.value))}>{[2, 3, 4].map(n => <option key={n} value={n}>{`≥ ${n} ${t(`sh.c${n}` as Key)}`}</option>)}</select></label>
        <button className="btn primary" onClick={run} disabled={busy || deficit <= 0}>{t('sh.plan')}</button>
      </div>
      {err && <div role="alert" className="errmsg">{err}</div>}
      {r && <>
        {!r.roster.feasible && <div role="alert" className="errmsg">■ {t('sh.infeasible')}</div>}
        <p className="num">{t('sh.fair')}: <strong>{r.roster.jain_fairness.toFixed(2)}</strong> ({t('sh.rot')}){r.static ? <> vs <strong>{r.static.jain_fairness.toFixed(2)}</strong> ({t('sh.static')})</> : null} · {t('sh.ever')}: <strong>{r.roster.loads_ever_shed}</strong> / {r.roster.eligible_loads}{r.static ? <> vs <strong>{r.static.loads_ever_shed}</strong></> : null} · {t('sh.maxens')}: <strong>{n1(r.roster.max_ens_mwh, 0)}</strong>{r.static ? <> vs <strong>{n1(r.static.max_ens_mwh, 0)}</strong></> : null}</p>
        <div className="tablewrap" style={{ maxHeight: 360 }}><table><thead><tr><th>{t('sh.pstart')}</th><th>{t('sh.pmw')}</th><th>{t('sh.ploads')}</th><th>{t('sh.pac')}</th></tr></thead>
          <tbody>{r.roster.periods.map(p => (
            <tr key={p.k}><td className="num">{p.start_h}</td><td className="num">{n1(p.shed_mw, 0)}{p.shortfall_mw > 0 ? ` (−${n1(p.shortfall_mw, 0)})` : ''}</td><td>{p.loads.join(', ')}</td>
              <td>{p.ac ? <span className="tag">{p.ac.converged && p.ac.overloads <= (r.roster.ac_base?.overloads ?? 0) ? `● ${t('sh.ac.ok')}` : `▲ ${t('sh.ac.bad')}: ${p.ac.overloads}`} · {n1(p.ac.max_loading_pct, 0)} %{p.ac_retries ? ` · ${t('sh.retries')} ×${p.ac_retries}` : ''}</span> : '–'}</td></tr>))}</tbody></table></div>
      </>}
    </div>
  );
}

function PrioTab({ model }: { model: string }) {
  const [p, setP] = useState<Prio | null>(null); const [err, setErr] = useState('');
  const load = () => post<Prio>('/ems/v1/shed/priorities', { model }).then(setP).catch(e => setErr((e as Error).message));
  return (
    <div>
      <button className="btn primary" onClick={() => void load()}>{t('sh.run')}</button>
      {err && <div role="alert" className="errmsg">{err}</div>}
      {p && <>
        {p.synthetic && <p className="muted">{t('sh.synth')}</p>}
        <div className="tablewrap"><table><thead><tr><th>{t('sh.class')}</th><th>{t('sh.n')}</th><th>{t('sh.mw')}</th></tr></thead>
          <tbody>{Object.entries(p.classes).map(([c, v]) => <tr key={c}><td>{c} · {t(`sh.c${c}` as Key)}</td><td className="num">{v.loads}</td><td className="num">{n1(v.mw, 0)}</td></tr>)}</tbody></table></div>
        <div className="tablewrap" style={{ maxHeight: 280, marginTop: 8 }}><table><thead><tr><th>{t('sh.load')}</th><th>{t('sh.bus')}</th><th>{t('sh.mw')}</th><th>{t('sh.class')}</th></tr></thead>
          <tbody>{p.loads.map(l => <tr key={l.load}><td>{l.load}</td><td>{l.bus}</td><td className="num">{n1(l.mw, 1)}</td><td className="num">{l.cls}</td></tr>)}</tbody></table></div>
      </>}
    </div>
  );
}
