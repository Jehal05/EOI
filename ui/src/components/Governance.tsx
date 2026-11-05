import { useCallback, useEffect, useState } from 'react';
import { api, post, atLeast, getSession } from '../lib/api';
import { t, type Key } from '../lib/i18n';

interface Sum { id: number; title: string; author: string; baseVersion: number; status: string; approvals: string[]; regions: string[]; changes: number; mergedVersion: number | null }
interface FC { field: string; old: unknown; new: unknown }
interface Chg { kind: string; mrid: string; op: string; fields: FC[] }
interface Conf { key: string; reason: string; base: unknown; ours: unknown; theirs: unknown }
interface Detail { proposal: Sum; required: number; changes: Chg[]; mergeStatus: { state: string; count?: number; note?: string }; conflicts: Conf[]; approvals: { user: string; comment: string }[]; rejections: { user: string; comment: string }[]; events: { ts_ms: number; user: string; action: string; detail: string }[] }
interface HConf { key: string; sources: string[]; fields: string[] }
interface ModelInfo { id: string; name: string; version: number; substations: number }
const MARK: Record<string, string> = { open: '◔', approved: '●', rejected: '✕', merged: '✔', withdrawn: '–' };
const KINDS = ['load', 'generator', 'shunt', 'switch', 'acLineSegment', 'transformer', 'substation', 'voltageLevel', 'connectivityNode'];
const show = (v: unknown) => v === null || v === undefined ? '–' : typeof v === 'object' ? JSON.stringify(v) : String(v);

type Tab = 'proposals' | 'new' | 'hier';

export function GovernanceView({ model }: { model: string }) {
  const [tab, setTab] = useState<Tab>('proposals');
  const eng = atLeast(getSession(), 'engineer');
  return (
    <div>
      <h1>{t('gv.title')}</h1>
      <div role="note" className="card" style={{ borderLeft: '6px solid var(--accent)', marginBottom: 10 }}>{t('gv.banner')}</div>
      <div className="seg" role="group" aria-label={t('gv.title')} style={{ marginBottom: 10 }}>
        {(['proposals', 'new', 'hier'] as Tab[]).map(x => <button key={x} aria-pressed={tab === x} onClick={() => setTab(x)}>{t(`gv.tab.${x}` as Key)}</button>)}
      </div>
      {!eng && <p className="muted">{t('gv.readonly')}</p>}
      {tab === 'proposals' && <Proposals model={model} eng={eng} />}
      {tab === 'new' && <NewProposal model={model} eng={eng} onDone={() => setTab('proposals')} />}
      {tab === 'hier' && <Hier eng={eng} />}
    </div>
  );
}

function Proposals({ model, eng }: { model: string; eng: boolean }) {
  const [list, setList] = useState<Sum[]>([]); const [req, setReq] = useState(1); const [sel, setSel] = useState<number | null>(null);
  const [d, setD] = useState<Detail | null>(null); const [msg, setMsg] = useState(''); const [res, setRes] = useState<Record<string, string>>({}); const [comment, setComment] = useState('');
  const load = useCallback(() => { api<{ required: number; proposals: Sum[] }>(`/cim/models/${model}/proposals`).then(r => { setList(r.proposals); setReq(r.required); }).catch(e => setMsg((e as Error).message)); }, [model]);
  const open = useCallback((id: number) => { setSel(id); setRes({}); api<Detail>(`/cim/models/${model}/proposals/${id}`).then(setD).catch(e => setMsg((e as Error).message)); }, [model]);
  useEffect(load, [load]);
  const act = (what: string, body: unknown = {}) => { if (sel === null) return; setMsg(''); post<{ version?: number }>(`/cim/models/${model}/proposals/${sel}/${what}`, body).then(r => { setMsg(what === 'merge' ? `${t('gv.merged')} v${r.version}` : ''); load(); open(sel); }).catch(e => { setMsg((e as Error).message); open(sel); }); };
  const nres = d ? d.conflicts.length : 0, allRes = d ? d.conflicts.every(c => res[c.key]) : false;
  return (
    <div className="row" style={{ gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <section className="card" style={{ flex: '1 1 320px' }} aria-label={t('gv.tab.proposals')}>
        <h2>{t('gv.tab.proposals')} <span className="muted" style={{ fontSize: 12 }}>({t('gv.required')}: {req})</span></h2>
        {!list.length ? <p className="muted">{t('gv.none')}</p> : (
          <table><thead><tr><th>#</th><th>{t('gv.f.title')}</th><th>{t('gv.f.author')}</th><th>{t('gv.f.status')}</th></tr></thead>
            <tbody>{list.map(p => <tr key={p.id}>
              <td className="num">{p.id}</td><td><button className="btn" onClick={() => open(p.id)} aria-label={`${t('gv.open')} ${p.id}`}>{p.title}</button></td><td>{p.author}</td>
              <td><span className="tag">{MARK[p.status]} {t(`gv.st.${p.status}` as Key)}</span></td></tr>)}</tbody></table>)}
      </section>
      <section className="card" style={{ flex: '2 1 460px' }} aria-label={t('gv.detail')}>
        {!d ? <p className="muted">{t('gv.pick')}</p> : (<>
          <h2>#{d.proposal.id} {d.proposal.title}</h2>
          <p>{t('gv.f.author')}: <strong>{d.proposal.author}</strong> · {t('gv.base')}: <span className="num">v{d.proposal.baseVersion}</span> · {t('gv.regions')}: {d.proposal.regions.join(', ')} · {t('gv.f.status')}: <span className="tag" role="status">{MARK[d.proposal.status]} {t(`gv.st.${d.proposal.status}` as Key)}</span></p>
          <p role="status">{d.mergeStatus.state === 'conflicts' ? `▲ ${t('gv.ms.conflicts')} (${d.mergeStatus.count})` : d.mergeStatus.state === 'base-lost' ? `■ ${t('gv.ms.lost')}` : d.mergeStatus.state === 'clean-merge' ? `● ${t('gv.ms.clean')}` : `● ${t('gv.ms.ff')}`}</p>
          <h3>{t('gv.changes')} ({d.changes.length})</h3>
          <table><thead><tr><th>{t('gv.f.op')}</th><th>{t('gv.f.element')}</th><th>{t('gv.f.field')}</th><th>{t('gv.f.old')}</th><th>{t('gv.f.new')}</th></tr></thead>
            <tbody>{d.changes.slice(0, 60).flatMap(c => (c.op === 'modify' ? c.fields : c.fields.slice(0, 1)).map((f, i) => <tr key={c.kind + c.mrid + f.field + i}><td>{c.op === 'add' ? '＋' : c.op === 'remove' ? '－' : '～'} {t(`gv.op.${c.op}` as Key)}</td><td>{c.kind} {c.mrid}</td><td>{c.op === 'modify' ? f.field : ''}</td><td className="num">{c.op === 'modify' ? show(f.old) : ''}</td><td className="num">{c.op === 'modify' ? show(f.new) : ''}</td></tr>))}</tbody></table>
          {d.changes.length > 60 && <p className="muted">{t('gv.more')}</p>}
          {nres > 0 && (<><h3>▲ {t('gv.conflicts')}</h3>
            <table><thead><tr><th>{t('gv.f.element')}</th><th>{t('gv.ours')}</th><th>{t('gv.theirs')}</th><th>{t('gv.choose')}</th></tr></thead>
              <tbody>{d.conflicts.map(c => <tr key={c.key}><td>{c.key}<br /><span className="muted">{c.reason}</span></td><td className="num">{show(c.ours)}</td><td className="num">{show(c.theirs)}</td>
                <td><select aria-label={`${t('gv.choose')} ${c.key}`} value={res[c.key] ?? ''} onChange={e => setRes({ ...res, [c.key]: e.target.value })}><option value="">–</option><option value="ours">{t('gv.ours')}</option><option value="theirs">{t('gv.theirs')}</option></select></td></tr>)}</tbody></table></>)}
          <h3>{t('gv.review')}</h3>
          <ul>{d.approvals.map(a => <li key={a.user}>● {t('gv.approved_by')} {a.user}{a.comment ? ` — ${a.comment}` : ''}</li>)}{d.rejections.map(a => <li key={a.user}>✕ {t('gv.rejected_by')} {a.user}{a.comment ? ` — ${a.comment}` : ''}</li>)}{!d.approvals.length && !d.rejections.length && <li className="muted">{t('gv.noreview')}</li>}</ul>
          {eng && ['open', 'approved'].includes(d.proposal.status) && (
            <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
              <label>{t('gv.comment')} <input value={comment} onChange={e => setComment(e.target.value)} maxLength={500} /></label>
              <button className="btn" onClick={() => act('approve', { comment })}>{t('gv.approve')}</button>
              <button className="btn" onClick={() => act('reject', { comment })}>{t('gv.reject')}</button>
              <button className="btn" onClick={() => act('withdraw')}>{t('gv.withdraw')}</button>
              <button className="btn primary" disabled={d.proposal.status !== 'approved' || (nres > 0 && !allRes)} onClick={() => act('merge', { resolutions: res })}>{t('gv.merge')}</button>
            </div>)}
          <h3>{t('gv.events')}</h3>
          <ul className="muted">{d.events.map((e, i) => <li key={i}>{new Date(e.ts_ms).toLocaleString()} · {e.user} · {e.action} {e.detail}</li>)}</ul>
        </>)}
        {msg && <div role="alert" className={msg.startsWith(t('gv.merged')) ? 'okmsg' : 'errmsg'}>{msg}</div>}
      </section>
    </div>
  );
}

interface Op { op: string; kind: string; mrid: string; fields: Record<string, unknown> }

function NewProposal({ model, eng, onDone }: { model: string; eng: boolean; onDone: () => void }) {
  const [cat, setCat] = useState<Record<string, { mrid: string }[]>>({}); const [kind, setKind] = useState(KINDS[0]); const [mrid, setMrid] = useState('');
  const [field, setField] = useState('p'); const [value, setValue] = useState(''); const [ops, setOps] = useState<Op[]>([]); const [title, setTitle] = useState(''); const [msg, setMsg] = useState('');
  useEffect(() => { api<Record<string, { mrid: string }[]>>(`/cim/models/${model}`).then(setCat).catch(() => undefined); }, [model]);
  const plural: Record<string, string> = { load: 'loads', generator: 'generators', shunt: 'shunts', switch: 'switches', acLineSegment: 'acLineSegments', transformer: 'transformers', substation: 'substations', voltageLevel: 'voltageLevels', connectivityNode: 'connectivityNodes' };
  const ids = (cat[plural[kind]] ?? []).map(x => x.mrid);
  const add = () => { let v: unknown = value; try { v = JSON.parse(value); } catch { /* plain string */ } setOps([...ops, { op: 'set', kind, mrid: mrid || ids[0], fields: { [field]: v } }]); };
  const send = () => { setMsg(''); post(`/cim/models/${model}/proposals`, { title, ops }).then(() => { setOps([]); setTitle(''); onDone(); }).catch(e => setMsg((e as Error).message)); };
  return (
    <section className="card" aria-label={t('gv.tab.new')}>
      <h2>{t('gv.tab.new')}</h2>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label>{t('gv.f.kind')} <select value={kind} onChange={e => { setKind(e.target.value); setMrid(''); }}>{KINDS.map(k => <option key={k}>{k}</option>)}</select></label>
        <label>{t('gv.f.element')} <select value={mrid || ids[0] || ''} onChange={e => setMrid(e.target.value)}>{ids.map(i => <option key={i}>{i}</option>)}</select></label>
        <label>{t('gv.f.field')} <input value={field} onChange={e => setField(e.target.value)} /></label>
        <label>{t('gv.f.new')} <input value={value} onChange={e => setValue(e.target.value)} /></label>
        <button className="btn" disabled={!eng || !field || value === ''} onClick={add}>{t('gv.addop')}</button>
      </div>
      {ops.length > 0 && <ul>{ops.map((o, i) => <li key={i} className="num">{o.kind} {o.mrid}: {JSON.stringify(o.fields)} <button className="btn" onClick={() => setOps(ops.filter((_, j) => j !== i))} aria-label={t('gv.remove')}>✕</button></li>)}</ul>}
      <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'flex-end' }}>
        <label style={{ flex: 1 }}>{t('gv.f.title')} <input style={{ width: '100%' }} value={title} onChange={e => setTitle(e.target.value)} maxLength={200} /></label>
        <button className="btn primary" disabled={!eng || !ops.length || !title.trim()} onClick={send}>{t('gv.submit')}</button>
      </div>
      {msg && <div role="alert" className="errmsg">{msg}</div>}
    </section>
  );
}

function Hier({ eng }: { eng: boolean }) {
  const [models, setModels] = useState<ModelInfo[]>([]); const [pick, setPick] = useState<string[]>([]); const [target, setTarget] = useState('merged-model'); const [from, setFrom] = useState('');
  const [out, setOut] = useState<string>(''); const [conf, setConf] = useState<HConf[]>([]); const [auth, setAuth] = useState<Record<string, string>>({}); const [msg, setMsg] = useState('');
  const load = () => api<ModelInfo[]>('/cim/models').then(setModels).catch(() => undefined);
  useEffect(() => { void load(); }, []);
  const run = (commit: boolean) => { setMsg(''); setOut(''); post<{ committed: boolean; version?: number; sources: string[]; stats: Record<string, number>; notes: string[] }>('/cim/hierarchy/merge', { target, children: pick.map(m => ({ model: m })), scalarsFrom: from || pick[0], authority: auth, commit })
    .then(r => { setConf([]); setOut(`${r.committed ? `✔ ${t('gv.committed')} v${r.version}` : `● ${t('gv.dry')}`}: ${Object.entries(r.stats).map(([k, v]) => `${k} ${v}`).join(', ')}`); if (r.committed) void load(); })
    .catch(e => { const m = (e as Error).message; setMsg(m); if (m.includes('disagree')) void fetchConf(); }); };
  // the conflict list travels in the error body; re-ask for it with a dry run through fetch so we can read it
  const fetchConf = () => { const s = getSession(); if (!s) return; void fetch('/cim/hierarchy/merge', { method: 'POST', headers: { Authorization: 'Bearer ' + s.token, 'Content-Type': 'application/json' }, body: JSON.stringify({ target, children: pick.map(m => ({ model: m })), scalarsFrom: from || pick[0], authority: auth }) }).then(r => r.json()).then((b: { conflicts?: HConf[] }) => setConf(b.conflicts ?? [])); };
  const split = () => { setMsg(''); post<{ children: { id: string }[] }>('/cim/models/ieee118/split?commit=1', {}).then(r => { setOut(`✔ ${t('gv.splitdone')}: ${r.children.map(c => c.id).join(', ')}`); void load(); }).catch(e => setMsg((e as Error).message)); };
  const toggle = (id: string) => setPick(pick.includes(id) ? pick.filter(x => x !== id) : [...pick, id]);
  return (
    <section className="card" aria-label={t('gv.tab.hier')}>
      <h2>{t('gv.tab.hier')}</h2>
      <p className="muted">{t('gv.hier.help')}</p>
      <div className="row" style={{ gap: 8, marginBottom: 8 }}><button className="btn" disabled={!eng} onClick={split}>{t('gv.split')}</button></div>
      <fieldset><legend>{t('gv.children')}</legend>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>{models.map(m => <label key={m.id}><input type="checkbox" checked={pick.includes(m.id)} onChange={() => toggle(m.id)} /> {m.id} <span className="muted">({m.substations})</span></label>)}</div></fieldset>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end', marginTop: 8 }}>
        <label>{t('gv.target')} <input value={target} onChange={e => setTarget(e.target.value)} maxLength={64} /></label>
        <label>{t('gv.scalars')} <select value={from} onChange={e => setFrom(e.target.value)}><option value="">{pick[0] ?? '–'}</option>{pick.map(p => <option key={p}>{p}</option>)}</select></label>
        <button className="btn" disabled={!eng || pick.length < 2 || !target} onClick={() => run(false)}>{t('gv.dryrun')}</button>
        <button className="btn primary" disabled={!eng || pick.length < 2 || !target} onClick={() => run(true)}>{t('gv.commit')}</button>
      </div>
      {conf.length > 0 && (<><h3>▲ {t('gv.conflicts')}</h3><table><thead><tr><th>{t('gv.f.element')}</th><th>{t('gv.f.field')}</th><th>{t('gv.authority')}</th></tr></thead>
        <tbody>{conf.map(c => <tr key={c.key}><td>{c.key}</td><td>{c.fields.join(', ')}</td><td><select aria-label={`${t('gv.authority')} ${c.key}`} value={auth[c.key] ?? ''} onChange={e => setAuth({ ...auth, [c.key]: e.target.value })}><option value="">–</option>{c.sources.map(s => <option key={s}>{s}</option>)}</select></td></tr>)}</tbody></table></>)}
      {out && <div role="status" className="okmsg">{out}</div>}
      {msg && <div role="alert" className="errmsg">{msg}</div>}
    </section>
  );
}
