import { useMemo, useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { useLive } from '../lib/store';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Cor {
  from_area: number; to_area: number; n_branches: number; base_flow_mw: number; status: string; dc_mw: number; ac_mw: number; atc_mw: number; ttc_mw: number; trm_mw: number;
  utilization_pct?: number; level?: 'ok' | 'watch' | 'low'; limit: { type: string; element: string | null; outage: string | null }; preexisting_excluded?: number;
}
interface Res { corridors: Cor[]; areas: { area: number; buses: number; gens: number }[]; wall_seconds: number; notes: string[] }
const NAME = (a: number) => String.fromCharCode(65 + a);
const MARK: Record<string, string> = { ok: '●', watch: '▲', low: '■' };

export function TcmView({ model }: { model: string }) {
  const live = useLive();
  const [parts, setParts] = useState(3);
  const [trm, setTrm] = useState(5);
  const [lim, setLim] = useState(1.0);
  const [res, setRes] = useState<Res | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const allowed = atLeast(getSession(), 'viewer');
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
    post<Res>('/ems/v1/tcm', { model, open_switches: snap.open, gen_p: snap.gen_p, load_p: snap.load_p, parts, trm_pct: trm, n1_limit: lim })
      .then(setRes).catch(e => setErr((e as Error).message)).finally(() => setBusy(false));
  };
  const why = (c: Cor) => `${t(`tcm.t.${c.limit.type}` as Key)}${c.limit.element ? ` · ${c.limit.element}` : ''}${c.limit.outage ? ` ${t('tcm.after')} ${c.limit.outage}` : ''}`;
  return (
    <div>
      <h1>{t('tcm.title')} {busy && <span className="muted">{t('tcm.running')}</span>}</h1>
      <div className="row" style={{ marginBottom: 12, gap: 16, flexWrap: 'wrap' }}>
        <label>{t('tcm.areas')} <select value={parts} onChange={e => setParts(Number(e.target.value))}>{[2, 3, 4, 5].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <label>{t('tcm.trm')} <select value={trm} onChange={e => setTrm(Number(e.target.value))}>{[0, 5, 10, 15].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <label>{t('tcm.n1')} <select value={lim} onChange={e => setLim(Number(e.target.value))}><option value={1.0}>100 %</option><option value={1.15}>115 %</option><option value={1.3}>130 %</option></select></label>
        <button className="btn primary" onClick={run} disabled={busy || !allowed}>{t('tcm.run')}</button>
      </div>
      {err && <div className="empty">{t('common.error')}: {err}</div>}
      {!res && !err && <div className="empty">{t('tcm.empty')}</div>}
      {res && (
        <div className="tablewrap" style={{ maxHeight: 'calc(100vh - 330px)', minHeight: 160 }}>
          <table>
            <thead><tr><th>{t('tcm.corridor')}</th><th>{t('tcm.flow')} MW</th><th>{t('tcm.ttc')} MW</th><th>{t('tcm.atc')} MW</th><th>{t('tcm.dc')} / {t('tcm.ac')} MW</th><th>{t('tcm.util')}</th><th>{t('tcm.level')}</th><th>{t('tcm.limit')}</th></tr></thead>
            <tbody>{res.corridors.map(c => (
              <tr key={`${c.from_area}-${c.to_area}`}>
                <td><strong>{NAME(c.from_area)} → {NAME(c.to_area)}</strong> <span className="muted num">({c.n_branches})</span></td>
                <td className="num">{n1(c.base_flow_mw, 0)}</td><td className="num">{n1(c.ttc_mw, 0)}</td><td className="num"><strong>{n1(c.atc_mw, 1)}</strong></td>
                <td className="num">{n1(c.dc_mw, 1)} / {n1(c.ac_mw, 1)}</td>
                <td className="num">{c.utilization_pct === undefined ? '–' : `${n1(c.utilization_pct, 0)} %`}</td>
                <td>{c.level ? <span className="tag">{MARK[c.level]} {t(`tcm.level.${c.level}` as Key)}</span> : '–'}</td>
                <td>{why(c)} {c.preexisting_excluded ? <span className="muted num">· {c.preexisting_excluded} {t('tcm.excluded')}</span> : null}</td>
              </tr>))}</tbody>
          </table>
        </div>)}
      {res && <p className="muted num" style={{ fontSize: 12 }}>{res.areas.map(a => `${NAME(a.area)}: ${a.buses}`).join(' · ')} · {n1(res.wall_seconds, 1)} s</p>}
      <p className="muted" style={{ fontSize: 12 }}>{t('tcm.note')}</p>
    </div>
  );
}
