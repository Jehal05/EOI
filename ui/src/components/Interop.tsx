import { useCallback, useEffect, useState } from 'react';
import { api, apiText, post, atLeast, getSession } from '../lib/api';
import { t } from '../lib/i18n';

interface ImportResult { id: string; version: number; hash: string; counts: Record<string, number>; warnings: string[]; validationWarnings: number }
interface Row { tag: string; kind: string; n: number; compared: number; withinPct: number | null; meanAbsDiff?: number | null; maxAbsDiff?: number; statusMismatches?: number; lastLegacy?: unknown; lastOurs?: unknown }
interface Report { enabled: boolean; samples: number; tolerance: { abs: number; rel: number }; tagsCompared: number; comparisons: number; agreementPct: number | null;
  ourTagsNeverSeenInLegacy: number; missingSample: string[]; legacyTagsUnknownHere: number; unknownSample: string[]; criterion?: { minAgreementPct: number; met: boolean }; worst: Row[] }

const FORMATS = [['cimxml', 'io.f.cimxml', '/cim/import/cimxml'], ['matpower', 'io.f.matpower', '/cim/import/matpower'], ['psse', 'io.f.psse', '/cim/import/psse']] as const;
const pct = (v: number | null | undefined) => v === null || v === undefined ? '–' : v.toFixed(2) + ' %';

function save(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/rdf+xml' }));
  const a = document.createElement('a'); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}

export function InteropView({ model }: { model: string }) {
  const eng = atLeast(getSession(), 'engineer');
  return (
    <div>
      <h1>{t('io.title')}</h1>
      <div role="note" className="card" style={{ borderLeft: '6px solid var(--accent)', marginBottom: 10 }}>{t('io.banner')}</div>
      <ExportCard model={model} />
      <ImportCard model={model} eng={eng} />
      <ParallelCard eng={eng} />
    </div>
  );
}

function ExportCard({ model }: { model: string }) {
  const [from, setFrom] = useState('1'); const [to, setTo] = useState('1'); const [msg, setMsg] = useState('');
  const dl = (path: string, name: string) => { setMsg(''); apiText(path).then(x => { save(name, x); setMsg(`${name}: ${(x.length / 1024).toFixed(0)} kB`); }).catch(e => setMsg((e as Error).message)); };
  return (
    <section className="card" aria-label={t('io.export')}>
      <h2>{t('io.export')}</h2>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <button className="btn" onClick={() => dl(`/cim/models/${model}/export.xml`, `${model}.xml`)}>{t('io.dl.full')}</button>
        <label>{t('io.from')} <input className="num" inputMode="numeric" size={4} value={from} onChange={e => setFrom(e.target.value)} /></label>
        <label>{t('io.to')} <input className="num" inputMode="numeric" size={4} value={to} onChange={e => setTo(e.target.value)} /></label>
        <button className="btn" onClick={() => dl(`/cim/models/${model}/difference.xml?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, `${model}-v${from}-v${to}.xml`)}>{t('io.dl.diff')}</button>
      </div>
      {msg && <div role="status" className="muted">{msg}</div>}
    </section>
  );
}

function ImportCard({ model, eng }: { model: string; eng: boolean }) {
  const [fmt, setFmt] = useState<string>('cimxml'); const [id, setId] = useState(''); const [text, setText] = useState(''); const [fname, setFname] = useState('');
  const [res, setRes] = useState<ImportResult | null>(null); const [err, setErr] = useState('');
  const run = () => {
    setErr(''); setRes(null);
    const f = FORMATS.find(x => x[0] === fmt)!;
    const path = fmt === 'cimxml' ? (id ? `${f[2]}?id=${encodeURIComponent(id)}` : f[2]) : `${f[2]}?id=${encodeURIComponent(id)}`;
    apiText(path, { method: 'POST', body: text, headers: { 'Content-Type': 'text/plain' } }).then(x => setRes(JSON.parse(x))).catch(e => setErr((e as Error).message));
  };
  const diff = () => {
    setErr(''); setRes(null);
    apiText(`/cim/models/${model}/apply-difference`, { method: 'POST', body: text, headers: { 'Content-Type': 'text/plain' } }).then(x => setRes(JSON.parse(x))).catch(e => setErr((e as Error).message));
  };
  const pick = (f: File | undefined) => { if (!f) return; setFname(f.name); f.text().then(setText); };
  return (
    <section className="card" aria-label={t('io.import')} style={{ marginTop: 10 }}>
      <h2>{t('io.import')}</h2>
      {!eng && <p className="muted">{t('io.readonly')}</p>}
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label>{t('io.format')} <select value={fmt} onChange={e => setFmt(e.target.value)} disabled={!eng}>{FORMATS.map(f => <option key={f[0]} value={f[0]}>{t(f[1])}</option>)}</select></label>
        <label>{t('io.modelid')} <input value={id} onChange={e => setId(e.target.value)} maxLength={64} disabled={!eng} /></label>
        <label>{t('io.file')} <input type="file" onChange={e => pick(e.target.files?.[0])} disabled={!eng} /></label>
        {fname && <span className="muted">{fname} · {(text.length / 1024).toFixed(0)} kB</span>}
      </div>
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button className="btn primary" disabled={!eng || !text || (fmt !== 'cimxml' && !id)} onClick={run}>{t('io.run')}</button>
        <button className="btn" disabled={!eng || !text || fmt !== 'cimxml'} onClick={diff}>{t('io.applydiff')} ({model})</button>
      </div>
      {err && <div role="alert" className="errmsg">▲ {err}</div>}
      {res && (<div role="status" className="okmsg">
        ✔ {t('io.done')} {res.id} v{res.version} · {Object.entries(res.counts).map(([k, v]) => `${v} ${k}`).join(', ')}
        {res.warnings.length > 0 && <ul>{res.warnings.slice(0, 20).map((w, i) => <li key={i}>▲ {w}</li>)}</ul>}
      </div>)}
    </section>
  );
}

function ParallelCard({ eng }: { eng: boolean }) {
  const admin = atLeast(getSession(), 'admin');
  const [rep, setRep] = useState<Report | null>(null); const [msg, setMsg] = useState(''); const [reason, setReason] = useState(''); const [min, setMin] = useState('99.5');
  const [json, setJson] = useState(''); const [abs, setAbs] = useState('0.5'); const [rel, setRel] = useState('0.005');
  const load = useCallback(() => { api<Report>(`/api/parallel?minPct=${encodeURIComponent(min)}`).then(setRep).catch(e => setMsg((e as Error).message)); }, [min]);
  useEffect(load, [load]);
  const sw = (enabled: boolean) => { setMsg(''); post('/api/parallel/switch', { enabled, reason }).then(() => { setReason(''); load(); }).catch(e => setMsg((e as Error).message)); };
  const send = () => {
    setMsg('');
    let samples: unknown; try { samples = JSON.parse(json); } catch { setMsg(t('io.badjson')); return; }
    post<{ accepted: number; unknownTags: number }>('/api/parallel/samples', { samples, tolAbs: Number(abs), tolRel: Number(rel), source: 'ui' })
      .then(r => { setMsg(`${t('io.accepted')}: ${r.accepted} · ${t('io.unknown')}: ${r.unknownTags}`); load(); }).catch(e => setMsg((e as Error).message));
  };
  return (
    <section className="card" aria-label={t('io.par')} style={{ marginTop: 10 }}>
      <h2>{t('io.par')}</h2>
      <p>{t('io.par.help')}</p>
      <p role="status"><strong>{rep?.enabled ? `■ ${t('io.par.on')}` : `○ ${t('io.par.off')}`}</strong></p>
      {admin && (<div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label>{t('io.reason')} <input value={reason} onChange={e => setReason(e.target.value)} maxLength={200} /></label>
        <button className="btn" disabled={reason.trim().length < 3} onClick={() => sw(!rep?.enabled)}>{rep?.enabled ? t('io.par.disable') : t('io.par.enable')}</button>
      </div>)}
      {eng && (<div style={{ marginTop: 8 }}>
        <label style={{ display: 'block' }}>{t('io.samples')}
          <textarea rows={4} style={{ width: '100%' }} value={json} onChange={e => setJson(e.target.value)} placeholder='[{"tag":"BR0001-F.P","v":40.2},{"tag":"BRK-BR0001-F.STATE","st":2}]' /></label>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <label>{t('io.tolabs')} <input className="num" inputMode="decimal" size={6} value={abs} onChange={e => setAbs(e.target.value)} /></label>
          <label>{t('io.tolrel')} <input className="num" inputMode="decimal" size={6} value={rel} onChange={e => setRel(e.target.value)} /></label>
          <button className="btn" disabled={!json} onClick={send}>{t('io.send')}</button>
        </div>
      </div>)}
      <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'flex-end' }}>
        <label>{t('io.minpct')} <input className="num" inputMode="decimal" size={6} value={min} onChange={e => setMin(e.target.value)} /></label>
        <button className="btn" onClick={load}>{t('io.refresh')}</button>
      </div>
      {msg && <div role="alert" className="errmsg">{msg}</div>}
      {rep && (<>
        <p><strong>{t('io.agree')}: <span className="num">{pct(rep.agreementPct)}</span></strong> · {t('io.compared')}: <span className="num">{rep.tagsCompared}</span> · {t('io.samplesn')}: <span className="num">{rep.samples}</span>
          {rep.criterion && <> · <span className="tag">{rep.criterion.met ? '✔' : '▲'} {t(rep.criterion.met ? 'io.met' : 'io.notmet')}</span></>}</p>
        <p className="muted">{t('io.missing')}: <span className="num">{rep.ourTagsNeverSeenInLegacy}</span> · {t('io.unknown')}: <span className="num">{rep.legacyTagsUnknownHere}</span></p>
        {rep.worst.length > 0 && (<table><thead><tr><th>{t('io.tag')}</th><th>{t('io.within')}</th><th>{t('io.maxdiff')}</th><th>{t('io.last')}</th></tr></thead>
          <tbody>{rep.worst.slice(0, 10).map(r => <tr key={r.tag}><td>{r.tag}</td><td className="num">{pct(r.withinPct)}</td>
            <td className="num">{r.kind === 'status' ? `${r.statusMismatches} ✕` : r.maxAbsDiff?.toFixed(3)}</td><td className="num">{String(r.lastLegacy ?? '–')} / {String(r.lastOurs ?? '–')}</td></tr>)}</tbody></table>)}
      </>)}
    </section>
  );
}
