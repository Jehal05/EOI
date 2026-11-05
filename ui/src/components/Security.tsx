import { useMemo, useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { useLive } from '../lib/store';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Sweep { cases: number; critical: number; total_excess_pct: number; worst_loading_pct: number; n_overloads: number; islanding_outages: number; base_overloads?: number }
interface Crit { outage: string; worst_loading_pct: number; islanded: boolean; lost_load_mw: number; overloads: { branch: string; loading_pct: number }[] }
interface Act { type: 'switching' | 'redispatch' | 'load_shed'; action: string; element: string; bus?: string; from_mw?: number; to_mw?: number; delta_mw?: number; mw?: number; from_bus?: string; to_bus?: string; reason: string }
interface Res {
  status: 'secure' | 'secured' | 'improved' | 'not_improved'; before: Sweep; after: Sweep; critical_before: Crit[]; critical_after?: Crit[]; actions: Act[];
  redispatch_mw?: number; shed_mw?: number; cost_change_per_h?: number; wall_seconds: number; notes: string[]; live_overrides?: { gens: number; loads: number };
}

export function SecurityView({ model }: { model: string }) {
  const live = useLive();
  const [em, setEm] = useState(1.15);
  const [sw, setSw] = useState(true);
  const [rd, setRd] = useState(true);
  const [sh, setSh] = useState(true);
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const allowed = atLeast(getSession(), 'operator');
  const snap = useMemo(() => {
    const gen_p: Record<string, number> = {}, load_p: Record<string, number> = {}, open: string[] = [];
    for (const [k, p] of live.pts) {
      if (p.q) continue;
      if (/^G\d+\.P$/.test(k) && p.v !== undefined) gen_p[k.slice(0, -2)] = p.v;
      else if (/^L\d+\.P$/.test(k) && p.v !== undefined) load_p[k.slice(0, -2)] = p.v;
      else if (k.endsWith('.STATE') && p.s === 1) open.push(k.replace('.STATE', ''));
    }
    return { gen_p, load_p, open: open.sort() };
  }, [live.pts, live.version]);
  const run = () => {
    setBusy(true); setErr(null);
    post<Res>('/ems/v1/security-plan', { model, open_switches: snap.open, gen_p: snap.gen_p, load_p: snap.load_p, emergency: em, use_switching: sw, use_redispatch: rd, use_shed: sh })
      .then(setRes).catch(e => setErr((e as Error).message)).finally(() => setBusy(false));
  };
  return (
    <div>
      <h1>{t('sec.title')} {busy && <span className="muted">{t('sec.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 16, flexWrap: 'wrap' }}>
        <label>{t('sec.limit')} <select value={em} onChange={e => setEm(Number(e.target.value))}><option value={1.0}>100 %</option><option value={1.15}>115 %</option><option value={1.3}>130 %</option></select></label>
        <label><input type="checkbox" checked={sw} onChange={e => setSw(e.target.checked)} /> {t('sec.sw')}</label>
        <label><input type="checkbox" checked={rd} onChange={e => setRd(e.target.checked)} /> {t('sec.rd')}</label>
        <label><input type="checkbox" checked={sh} onChange={e => setSh(e.target.checked)} /> {t('sec.shed')}</label>
        <button className="btn primary" onClick={run} disabled={busy || !allowed}>{t('sec.run')}</button>
        <span className="muted">{t('sec.live')}: {Object.keys(snap.gen_p).length} / {Object.keys(snap.load_p).length}</span>
      </div>
      {err && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && <div className="empty">{t('sec.empty')}</div>}
      {res && (<>
        <div className="card" style={{ marginBottom: 12 }} role="status">
          <strong>{t(`sec.status.${res.status}` as Key)}</strong> <span className="muted num">· {n1(res.wall_seconds, 1)} s</span>
          {res.status === 'secure' && <span className="muted"> · {t('sec.secure')}</span>}
          {res.notes.map(n => <div key={n} className="muted">{n}</div>)}
        </div>
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <div className="card kpi"><div className="l">{t('sec.crit')}</div><div className="v num">{res.before.critical} → {res.after.critical}</div><div className="s">{t('sec.before')} → {t('sec.after')}</div></div>
          <div className="card kpi"><div className="l">{t('sec.excess')}</div><div className="v num">{n1(res.before.total_excess_pct, 0)} → {n1(res.after.total_excess_pct, 0)} %</div><div className="s num">max {n1(res.before.worst_loading_pct, 0)} → {n1(res.after.worst_loading_pct, 0)} %</div></div>
          <div className="card kpi"><div className="l">{t('sec.redis')}</div><div className="v num">{n1(res.redispatch_mw ?? 0, 0)} MW</div><div className="s num">{t('sec.cost')}: {n1(res.cost_change_per_h ?? 0, 0)} /h</div></div>
          <div className="card kpi"><div className="l">{t('sec.shedmw')}</div><div className="v num">{n1(res.shed_mw ?? 0, 1)} MW</div><div className="s num">{res.actions.filter(a => a.type === 'switching').length} {t('sec.sw')}</div></div>
        </div>
        <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,3fr) minmax(0,2fr)' }}>
          <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 430px)', minHeight: 160 }}>
            <h2 style={{ padding: '8px 12px 0' }}>{t('sec.plan')} ({res.actions.length})</h2>
            <table>
              <thead><tr><th>{t('sec.type')}</th><th>{t('sec.element')}</th><th>{t('sec.detail')}</th></tr></thead>
              <tbody>{res.actions.map((a, i) => (
                <tr key={`${a.type}${a.element}${i}`}>
                  <td><span className="tag">{a.type === 'switching' ? t('sec.sw') : a.type === 'redispatch' ? t('sec.rd') : t('sec.shed')}</span></td>
                  <td><strong>{a.element}</strong> <span className="muted">{(a.bus ?? '').replace('Busbar ', 'B')}</span></td>
                  <td className="num">{a.type === 'redispatch' ? `${a.delta_mw! >= 0 ? '▲' : '▼'} ${n1(Math.abs(a.delta_mw!), 1)} MW (${n1(a.from_mw!, 0)} → ${n1(a.to_mw!, 0)})` : a.type === 'load_shed' ? `${n1(a.mw!, 1)} MW` : `${a.action} ${(a.from_bus ?? '').replace('Busbar ', 'B')}–${(a.to_bus ?? '').replace('Busbar ', 'B')}`}</td>
                </tr>))}</tbody>
            </table>
          </div>
          <div className="card"><h2>{res.critical_after?.length ? t('sec.remaining') : t('sec.crittitle')}</h2>
            <table><tbody>{(res.critical_after?.length ? res.critical_after : res.critical_before).slice(0, 8).map(c => (
              <tr key={c.outage}><td className="mono">{c.outage}{c.islanded ? ' ⌀' : ''}</td><td className="num warn">{n1(c.worst_loading_pct, 0)} %</td><td className="muted">{c.overloads[0]?.branch}</td></tr>))}</tbody></table>
          </div>
        </div>
      </>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('sec.note')}</p>
    </div>
  );
}
