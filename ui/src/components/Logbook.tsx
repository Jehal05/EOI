import { useEffect, useState } from 'react';
import { api, post, atLeast, getSession } from '../lib/api';
import { t, type Key } from '../lib/i18n';

interface Entry { n: number; ts: number; user: string; category: string; text: string; amends?: number; ref?: string }
interface Snap { alarms: { active: number; unacked: number; shelved: number }; priority_alarms: { id: number; text: string; prio: number }[]; outages: { id: string; status: string; equipment: string[] }[]; comms: { substation: string; state: string; stale: number; points: number }[]; ai_enabled: boolean; notable_log: Entry[]; feed_lost: boolean }
interface Hand { n: number; ts: number; from: string; to?: string; notes: string; status: string; acceptedBy?: string; acceptedTs?: number; acceptNote?: string; snapshot: Snap | null }
interface Sub { substation: string; points: number; stale: number; state: string; since?: number }
interface Comms { substations: Sub[]; feed: { monitored: boolean; lost: boolean; heartbeat_age_ms: number; timeout_ms: number } }
const CATS = ['general', 'operation', 'abnormal', 'safety', 'equipment'];
const MARK: Record<string, string> = { ok: '●', degraded: '▲', lost: '■' };
const fmt = (ms: number) => new Date(ms).toLocaleString([], { dateStyle: 'short', timeStyle: 'medium' });

function SnapView({ s }: { s: Snap }) {
  return (
    <div>
      <p className="num">{t('hd.alarms')}: <strong>{s.alarms.active}</strong> · {t('hd.unacked')}: <strong>{s.alarms.unacked}</strong> · {t('hd.shelved')}: <strong>{s.alarms.shelved}</strong> · {t('hd.ai')}: <strong>{s.ai_enabled ? `● ${t('hd.on')}` : `■ ${t('hd.off')}`}</strong></p>
      {s.priority_alarms.length > 0 && <><h3>{t('hd.prio')}</h3><ul>{s.priority_alarms.map(a => <li key={a.id}>P{a.prio} · {a.text}</li>)}</ul></>}
      {s.outages.length > 0 && <><h3>{t('hd.outages')}</h3><ul>{s.outages.map(o => <li key={o.id}>{o.id} · {t(`out.st.${o.status}` as Key)} · {o.equipment.join(', ')}</li>)}</ul></>}
      {s.comms.length > 0 && <><h3>{t('hd.comms')}</h3><p className="num">{s.comms.length} {t('cm.sub')} — {s.feed_lost ? t('cm.feed.lost') : s.comms.slice(0, 5).map(c => `${MARK[c.state]} ${c.substation}`).join(', ')}{s.comms.length > 5 && !s.feed_lost ? ' …' : ''}</p></>}
      {s.notable_log.length > 0 && <><h3>{t('hd.notable')}</h3><ul>{s.notable_log.map(e => <li key={e.n}>#{e.n} [{t(`lb.cat.${e.category}` as Key)}] {e.user}: {e.text}</li>)}</ul></>}
    </div>
  );
}

export function LogbookView() {
  const [tab, setTab] = useState<'log' | 'hand' | 'comms'>('log');
  const me = getSession(); const canWrite = atLeast(me, 'operator');
  return (
    <div>
      <h1>{t('lb.title')}</h1>
      <div role="tablist" className="row" style={{ gap: 6, marginBottom: 10 }}>
        {(['log', 'hand', 'comms'] as const).map(k => <button key={k} role="tab" aria-selected={tab === k} className={'btn' + (tab === k ? ' primary' : '')} onClick={() => setTab(k)}>{t(`lb.tab.${k}` as Key)}</button>)}
      </div>
      {tab === 'log' && <LogTab canWrite={canWrite} />}
      {tab === 'hand' && <HandTab canWrite={canWrite} me={me?.user ?? ''} />}
      {tab === 'comms' && <CommsTab />}
    </div>
  );
}

function LogTab({ canWrite }: { canWrite: boolean }) {
  const [rows, setRows] = useState<Entry[]>([]);
  const [cat, setCat] = useState(''); const [q, setQ] = useState('');
  const [nc, setNc] = useState('general'); const [text, setText] = useState(''); const [ref, setRef] = useState(''); const [amends, setAmends] = useState(0);
  const [err, setErr] = useState('');
  const load = () => api<{ entries: Entry[] }>(`/api/log?limit=200${cat ? `&category=${cat}` : ''}${q ? `&q=${encodeURIComponent(q)}` : ''}`).then(r => setRows(r.entries)).catch(() => undefined);
  useEffect(() => { load(); const h = setInterval(load, 8000); return () => clearInterval(h); }, [cat, q]);
  const add = () => post('/api/log', { category: nc, text, ref: ref || undefined, amends: amends || undefined }).then(() => { setText(''); setRef(''); setAmends(0); setErr(''); load(); }).catch(e => setErr((e as Error).message));
  return (
    <div>
      {canWrite && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('lb.add')}>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label>{t('lb.cat')} <select value={nc} onChange={e => setNc(e.target.value)}>{CATS.map(c => <option key={c} value={c}>{t(`lb.cat.${c}` as Key)}</option>)}</select></label>
            <label>{t('lb.text')} <input value={text} onChange={e => setText(e.target.value)} style={{ width: 360 }} maxLength={1000} /></label>
            <label>{t('lb.ref')} <input value={ref} onChange={e => setRef(e.target.value)} style={{ width: 140 }} maxLength={64} /></label>
            {amends > 0 && <span className="tag">{t('lb.amends')} #{amends} <button className="btn" onClick={() => setAmends(0)} aria-label="✕">✕</button></span>}
            <button className="btn primary" disabled={text.trim().length < 3} onClick={() => void add()}>{t('lb.add')}</button>
          </div>
          {err && <div role="alert" className="errmsg" style={{ marginTop: 6 }}>{err}</div>}
        </section>)}
      <div className="row" style={{ gap: 10, marginBottom: 8, flexWrap: 'wrap' }}>
        <label>{t('lb.cat')} <select value={cat} onChange={e => setCat(e.target.value)}><option value="">{t('lb.all')}</option>{CATS.map(c => <option key={c} value={c}>{t(`lb.cat.${c}` as Key)}</option>)}</select></label>
        <label>{t('lb.search')} <input value={q} onChange={e => setQ(e.target.value)} style={{ width: 200 }} /></label>
      </div>
      {!rows.length ? <div className="empty">{t('lb.none')}</div> : (
        <div className="tablewrap" style={{ maxHeight: 420 }}>
          <table><thead><tr><th>#</th><th>{t('lb.time')}</th><th>{t('lb.user')}</th><th>{t('lb.cat')}</th><th>{t('lb.text')}</th>{canWrite && <th><span className="sr-only">{t('lb.amend')}</span></th>}</tr></thead>
            <tbody>{rows.map(e => (
              <tr key={e.n}><td className="num">{e.n}</td><td className="num">{fmt(e.ts)}</td><td>{e.user}</td>
                <td><span className="tag">{e.category === 'safety' ? '■' : e.category === 'abnormal' ? '▲' : '●'} {t(`lb.cat.${e.category}` as Key)}</span></td>
                <td>{e.text}{e.ref ? <span className="muted"> · {e.ref}</span> : null}{e.amends ? <div className="muted">{t('lb.amends')} #{e.amends}</div> : null}</td>
                {canWrite && <td><button className="btn" onClick={() => setAmends(e.n)}>{t('lb.amend')}</button></td>}</tr>))}</tbody></table>
        </div>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('lb.append')}</p>
    </div>
  );
}

function HandTab({ canWrite, me }: { canWrite: boolean; me: string }) {
  const [pend, setPend] = useState<Hand | null>(null); const [hist, setHist] = useState<Hand[]>([]);
  const [draft, setDraft] = useState<Snap | null>(null);
  const [notes, setNotes] = useState(''); const [to, setTo] = useState(''); const [note, setNote] = useState(''); const [err, setErr] = useState('');
  const load = () => {
    api<{ pending: Hand | null; history: Hand[] }>('/api/handover').then(r => { setPend(r.pending); setHist(r.history.filter(h => h.status === 'accepted')); }).catch(() => undefined);
    if (canWrite) api<Snap>('/api/handover/draft').then(setDraft).catch(() => undefined);
  };
  useEffect(() => { load(); const h = setInterval(load, 8000); return () => clearInterval(h); }, []);
  const issue = () => post('/api/handover', { notes, to: to || undefined }).then(() => { setNotes(''); setTo(''); setErr(''); load(); }).catch(e => setErr((e as Error).message));
  const accept = (n: number) => post('/api/handover/accept', { n, note }).then(() => { setNote(''); setErr(''); load(); }).catch(e => setErr((e as Error).message));
  return (
    <div>
      {err && <div role="alert" className="errmsg">{err}</div>}
      <section className="card" style={{ marginBottom: 12 }} aria-label={t('hd.pending')}>
        <h2>{t('hd.pending')}</h2>
        {!pend ? <p className="muted">{t('hd.none')}</p> : (
          <div>
            <p><strong>#{pend.n}</strong> · {t('hd.from')} {pend.from}{pend.to ? ` → ${pend.to}` : ''} · <span className="num">{fmt(pend.ts)}</span> <span className="tag">◔ {t('hd.waiting')}</span></p>
            <p>{pend.notes}</p>
            {pend.snapshot && <SnapView s={pend.snapshot} />}
            {canWrite && pend.from !== me && (<div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <input aria-label={t('hd.note')} placeholder={t('hd.note')} value={note} onChange={e => setNote(e.target.value)} style={{ width: 280 }} />
              <button className="btn primary" onClick={() => void accept(pend.n)}>{t('hd.accept')}</button></div>)}
            {pend.from === me && <p className="muted">{t('hd.self')}</p>}
          </div>)}
      </section>
      {canWrite && !pend && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('hd.issue')}>
          <h2>{t('hd.issue')}</h2>
          {draft && <><h3>{t('hd.snapshot')}</h3><SnapView s={draft} /></>}
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end', marginTop: 8 }}>
            <label>{t('hd.notes')} <input value={notes} onChange={e => setNotes(e.target.value)} style={{ width: 420 }} maxLength={2000} /></label>
            <label>{t('hd.to')} <input value={to} onChange={e => setTo(e.target.value)} style={{ width: 140 }} /></label>
            <button className="btn primary" disabled={notes.trim().length < 3} onClick={() => void issue()}>{t('hd.issue')}</button>
          </div>
        </section>)}
      {hist.length > 0 && (<section aria-label={t('hd.history')}><h2>{t('hd.history')}</h2>
        <div className="tablewrap" style={{ maxHeight: 220 }}><table><thead><tr><th>#</th><th>{t('hd.from')}</th><th>{t('hd.accepted')}</th><th>{t('hd.notes')}</th></tr></thead>
          <tbody>{hist.map(h => <tr key={h.n}><td className="num">{h.n}</td><td>{h.from}<div className="muted num">{fmt(h.ts)}</div></td><td>● {h.acceptedBy}<div className="muted num">{h.acceptedTs ? fmt(h.acceptedTs) : ''}</div></td><td>{h.notes}</td></tr>)}</tbody></table></div></section>)}
    </div>
  );
}

function CommsTab() {
  const [c, setC] = useState<Comms | null>(null); const [only, setOnly] = useState(false);
  const load = () => api<Comms>('/api/comms').then(setC).catch(() => undefined);
  useEffect(() => { load(); const h = setInterval(load, 3000); return () => clearInterval(h); }, []);
  if (!c) return <div className="empty">…</div>;
  const f = c.feed; const rows = c.substations.filter(s => !only || s.state !== 'ok');
  const feedState = !f.monitored ? t('cm.feed.unmonitored') : f.lost ? `■ ${t('cm.feed.lost')}` : `● ${t('cm.feed.ok')}`;
  return (
    <div>
      <p><strong>{t('cm.feed')}:</strong> {feedState} {f.monitored && <span className="muted num">· {t('cm.age')} {(f.heartbeat_age_ms / 1000).toFixed(1)} s</span>}</p>
      <label><input type="checkbox" checked={only} onChange={e => setOnly(e.target.checked)} /> {t('cm.only')}</label>
      <div className="tablewrap" style={{ maxHeight: 420, marginTop: 8 }}>
        <table><thead><tr><th>{t('cm.sub')}</th><th>{t('cm.state')}</th><th>{t('cm.stale')}</th><th>{t('cm.points')}</th><th>{t('cm.since')}</th></tr></thead>
          <tbody>{rows.map(s => <tr key={s.substation}><td>{s.substation}</td><td><span className="tag">{MARK[s.state]} {t(`cm.${s.state}` as Key)}</span></td><td className="num">{s.stale}</td><td className="num">{s.points}</td><td className="num">{s.since ? fmt(s.since) : '–'}</td></tr>)}</tbody></table>
      </div>
      <p className="muted" style={{ fontSize: 12 }}>{t('cm.footer')}</p>
    </div>
  );
}
