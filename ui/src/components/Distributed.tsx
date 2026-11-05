import { useState } from 'react';
import { post, atLeast, getSession } from '../lib/api';
import { t } from '../lib/i18n';

interface Peer { peer: string; cases: number; ok: boolean; ms: number | null; reassigned_to: string | null; error: string | null }
interface Res { cases?: number; n_ac_verified?: number; wall_seconds: number; peers_configured: number; version: number;
  distributed: { peers: Peer[]; shards: number; wall_seconds?: number }; results: { outage: string; severity: number; converged: boolean }[] }

export function DistributedCard({ model }: { model: string }) {
  const [res, setRes] = useState<Res | null>(null); const [busy, setBusy] = useState(false); const [err, setErr] = useState(''); const [kind, setKind] = useState<'n1' | 'n2'>('n1');
  const ok = atLeast(getSession(), 'viewer');
  const run = () => { setBusy(true); setErr(''); post<Res>('/ems/v1/contingency/distributed', { model, kind, top: 8 }).then(setRes).catch(e => setErr((e as Error).message)).finally(() => setBusy(false)); };
  return (
    <section className="card" aria-label={t('ds.title')} style={{ marginBottom: 12 }}>
      <h2>{t('ds.title')}</h2>
      <p className="muted">{t('ds.help')}</p>
      <div className="row" style={{ gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <label>{t('ds.kind')} <select value={kind} onChange={e => setKind(e.target.value as 'n1' | 'n2')}><option value="n1">N-1</option><option value="n2">N-2</option></select></label>
        <button className="btn" disabled={!ok || busy} onClick={run}>{busy ? t('ds.running') : t('ds.run')}</button>
      </div>
      {err && <div role="alert" className="errmsg">▲ {err}</div>}
      {res && (<div role="status">
        <p>{t('ds.peers')}: <span className="num">{res.peers_configured}</span> · {t('ds.shards')}: <span className="num">{res.distributed.shards}</span> · {t('cont.wall')}: <span className="num">{(res.distributed.wall_seconds ?? res.wall_seconds).toFixed(1)} s</span> · v<span className="num">{res.version}</span></p>
        <table><thead><tr><th>{t('ds.worker')}</th><th>{t('ds.cases')}</th><th>{t('ds.status')}</th></tr></thead>
          <tbody>{res.distributed.peers.map(p => <tr key={p.peer}><td>{p.peer}</td><td className="num">{p.cases}</td>
            <td>{p.ok ? `✔ ${t('ds.ok')} (${p.ms === null ? '–' : p.ms.toFixed(0)} ms)` : `▲ ${t('ds.failed')} → ${p.reassigned_to}`}</td></tr>)}</tbody></table>
        <h3>{t('ds.worst')}</h3>
        <ul>{res.results.slice(0, 5).map(r => <li key={r.outage}><span className="num">{r.outage}</span> — {r.converged ? '' : `■ ${t('ds.nonconv')} `}<span className="num">{r.severity.toFixed(1)}</span></li>)}</ul>
      </div>)}
    </section>
  );
}
