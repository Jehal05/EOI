import { useEffect, useMemo, useState } from 'react';
import { api, post, atLeast, getSession } from '../lib/api';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Study { verdict: string; islanded_load_mw?: number; n_new_critical?: number; new_critical?: { outage: string; severity: number; worst: { branch: string; loading_pct: number } | null }[]; base_violations?: { kind: string; detail: string }[]; worst_loading_pct?: number; error?: string }
interface Out { id: string; n: number; equipment: string[]; start: number; end: number; reason: string; requester: string; status: string; study: Study | null; decidedBy?: string; decisionNote?: string }
interface Step { seq: number; manual: boolean; text?: string; action?: string; switch?: string; name?: string; substation?: string; kv?: number; safe?: boolean; check?: { max_loading_pct: number; islanded_load_mw: number; new_overloads: { branch: string; loading_pct: number }[]; converged: boolean }; interlock?: string | null }
interface Plan { status: string; mode: string; steps: Step[]; plan_hash: string; notes: string[] }
interface Model { acLineSegments: { mrid: string; name: string }[]; transformers: { mrid: string; name: string }[] }
const MARK: Record<string, string> = { requested: '◔', approved: '●', rejected: '✕', cancelled: '–', active: '▶', completed: '✔' };
const VMARK: Record<string, string> = { secure: '●', restricted: '▲', unsafe: '■', invalid: '■', unavailable: '?' };
const fmtT = (ms: number) => new Date(ms).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
const pad = (n: number) => String(n).padStart(2, '0');
const localInput = (ms: number) => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };

function Timeline({ rows }: { rows: Out[] }) {
  const live = rows.filter(r => ['requested', 'approved', 'active'].includes(r.status));
  if (!live.length) return null;
  const t0 = Math.min(...live.map(r => r.start)), t1 = Math.max(...live.map(r => r.end)), W = 760, L = 80, rowH = 22;
  const x = (v: number) => L + ((v - t0) / (t1 - t0 || 1)) * (W - L - 8);
  return (
    <svg viewBox={`0 0 ${W} ${live.length * rowH + 22}`} role="img" aria-label={t('out.timeline')} style={{ width: '100%', height: 'auto', maxHeight: 220 }}>
      {live.map((r, i) => (
        <g key={r.id}>
          <text x={4} y={i * rowH + 15} fontSize="11" fill="var(--text)">{r.id}</text>
          <rect x={x(r.start)} y={i * rowH + 3} width={Math.max(4, x(r.end) - x(r.start))} height={14} fill="var(--accent)" opacity={r.status === 'requested' ? 0.35 : 0.8} stroke="var(--accent)" strokeDasharray={r.status === 'requested' ? '4 3' : ''} />
          <text x={x(r.start) + 4} y={i * rowH + 14} fontSize="10" fill="var(--text)">{MARK[r.status]} {r.equipment.join(', ')}</text>
        </g>))}
      <text x={L} y={live.length * rowH + 16} fontSize="11" fill="var(--muted)">{fmtT(t0)}</text>
      <text x={W - 8} y={live.length * rowH + 16} fontSize="11" fill="var(--muted)" textAnchor="end">{fmtT(t1)}</text>
    </svg>
  );
}

export function OutagesView({ model }: { model: string }) {
  const [rows, setRows] = useState<Out[]>([]);
  const [eqs, setEqs] = useState<string[]>([]);
  const [eqIn, setEqIn] = useState('');
  const [start, setStart] = useState(localInput(Date.now() + 3_600_000));
  const [dur, setDur] = useState(4);
  const [reason, setReason] = useState('');
  const [catalog, setCatalog] = useState<string[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; study?: Study } | null>(null);
  const [busy, setBusy] = useState(false);
  const [dec, setDec] = useState<{ n: number; decision: 'approve' | 'reject'; verdict: string } | null>(null);
  const [note, setNote] = useState('');
  const [override, setOverride] = useState(false);
  const [plan, setPlan] = useState<{ id: string; mode: string; data: Plan } | null>(null);
  const me = getSession();
  const canReq = atLeast(me, 'operator'), canDecide = atLeast(me, 'engineer'), isAdmin = atLeast(me, 'admin');
  const load = () => api<{ outages: Out[] }>('/api/outages').then(r => setRows([...r.outages].reverse())).catch(() => undefined);
  useEffect(() => { load(); const h = setInterval(load, 8000); return () => clearInterval(h); }, []);
  useEffect(() => { api<Model>(`/cim/models/${model}`).then(m => setCatalog([...m.acLineSegments, ...m.transformers].map(e => e.mrid))).catch(() => undefined); }, [model]);
  const known = useMemo(() => new Set(catalog), [catalog]);
  const addEq = () => { const v = eqIn.trim(); if (v && !eqs.includes(v) && eqs.length < 10) setEqs([...eqs, v]); setEqIn(''); };
  const submit = () => {
    if (!eqs.length) { setMsg({ ok: false, text: t('out.err.eq') }); return; }
    const s = new Date(start).getTime();
    setBusy(true); setMsg(null);
    post<{ study: Study }>('/api/outages', { equipment: eqs, start: s, end: s + dur * 3_600_000, reason })
      .then(r => { setMsg({ ok: true, text: t('out.submit'), study: r.study }); setEqs([]); setReason(''); load(); })
      .catch(e => setMsg({ ok: false, text: (e as Error).message })).finally(() => setBusy(false));
  };
  const act = (path: string, n: number, extra: object = {}) => post(`/api/outages/${path}`, { n, ...extra }).then(() => { setDec(null); setNote(''); setOverride(false); load(); }).catch(e => setMsg({ ok: false, text: (e as Error).message }));
  const showPlan = (r: Out, mode: 'isolate' | 'restore') => { setPlan(null); post<Plan>('/api/outages/plan', { n: r.n, mode }).then(d => setPlan({ id: r.id, mode, data: d })).catch(e => setMsg({ ok: false, text: (e as Error).message })); };
  return (
    <div>
      <h1>{t('out.title')}</h1>
      {canReq && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('out.new')}>
          <h2>{t('out.new')}</h2>
          <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label>{t('out.eq')} <input list="eqlist" value={eqIn} onChange={e => setEqIn(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addEq(); } }} style={{ width: 120 }} />
              <datalist id="eqlist">{catalog.map(c => <option key={c} value={c} />)}</datalist></label>
            <button className="btn" onClick={addEq}>{t('out.add')}</button>
            <span>{eqs.map(e => <button key={e} className="tag" onClick={() => setEqs(eqs.filter(x => x !== e))} aria-label={`${e} ✕`}>{known.size && !known.has(e) ? '▲ ' : ''}{e} ✕</button>)}</span>
            <label>{t('out.start')} <input type="datetime-local" value={start} onChange={e => setStart(e.target.value)} /></label>
            <label>{t('out.dur')} <select value={dur} onChange={e => setDur(Number(e.target.value))}>{[1, 2, 4, 8, 24, 72].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
            <label>{t('out.reason')} <input value={reason} onChange={e => setReason(e.target.value)} style={{ width: 220 }} /></label>
            <button className="btn primary" onClick={submit} disabled={busy || reason.trim().length < 3}>{t('out.submit')}</button>
          </div>
          {msg && <div role="status" className={msg.ok ? 'okmsg' : 'errmsg'} style={{ marginTop: 8 }}>{msg.ok ? `${VMARK[msg.study?.verdict ?? 'unavailable']} ${t(`out.v.${msg.study?.verdict ?? 'unavailable'}` as Key)}` : msg.text}</div>}
        </section>)}
      <div className="card" style={{ marginBottom: 12 }}><Timeline rows={rows} /></div>
      {!rows.length && <div className="empty">{t('out.none')}</div>}
      {rows.length > 0 && (
        <div className="tablewrap" style={{ maxHeight: 320, marginBottom: 12 }}>
          <table>
            <thead><tr><th>{t('out.id')}</th><th>{t('out.eq')}</th><th>{t('out.window')}</th><th>{t('out.by')}</th><th>{t('out.status')}</th><th>{t('out.verdict')}</th><th>{t('out.actions')}</th></tr></thead>
            <tbody>{rows.map(r => {
              const v = r.study?.verdict ?? 'unavailable';
              return (
                <tr key={r.id}>
                  <td><strong>{r.id}</strong></td><td>{r.equipment.join(', ')}<div className="muted">{r.reason}</div></td>
                  <td className="num">{fmtT(r.start)} – {fmtT(r.end)}</td><td>{r.requester}{r.decidedBy ? <div className="muted">{r.decidedBy}</div> : null}</td>
                  <td><span className="tag">{MARK[r.status]} {t(`out.st.${r.status}` as Key)}</span></td>
                  <td><span className="tag">{VMARK[v]} {t(`out.v.${v}` as Key)}</span>{r.study && r.study.n_new_critical ? <div className="muted num">{r.study.n_new_critical} {t('out.newcrit')}</div> : null}{r.study?.islanded_load_mw ? <div className="muted num">{t('out.islanded')}: {n1(r.study.islanded_load_mw, 1)} MW</div> : null}</td>
                  <td>
                    <span className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                      {r.status === 'requested' && canDecide && r.requester !== me?.user && <><button className="btn" onClick={() => setDec({ n: r.n, decision: 'approve', verdict: v })}>{t('out.approve')}</button><button className="btn" onClick={() => setDec({ n: r.n, decision: 'reject', verdict: v })}>{t('out.reject')}</button></>}
                      {r.status === 'approved' && canReq && <button className="btn" onClick={() => void act('start', r.n)}>{t('out.begin')}</button>}
                      {r.status === 'active' && canReq && <button className="btn" onClick={() => void act('complete', r.n)}>{t('out.finish')}</button>}
                      {(r.status === 'requested' || r.status === 'approved') && canReq && <button className="btn" onClick={() => void act('cancel', r.n)}>{t('out.cancel')}</button>}
                      {['approved', 'active'].includes(r.status) && <><button className="btn" onClick={() => showPlan(r, 'isolate')}>{t('out.plan')}</button><button className="btn" onClick={() => showPlan(r, 'restore')}>{t('out.restore')}</button></>}
                    </span>
                  </td>
                </tr>);
            })}</tbody>
          </table>
        </div>)}
      {dec && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('out.note')}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <input aria-label={t('out.note')} placeholder={t('out.note')} value={note} onChange={e => setNote(e.target.value)} style={{ width: 280 }} />
            {dec.decision === 'approve' && dec.verdict !== 'secure' && isAdmin && <label><input type="checkbox" checked={override} onChange={e => setOverride(e.target.checked)} /> {t('out.override')}</label>}
            <button className="btn primary" disabled={note.trim().length < 3} onClick={() => void act('decision', dec.n, { decision: dec.decision, note, override })}>{t('out.confirm')}</button>
            <button className="btn" onClick={() => setDec(null)}>{t('out.cancel')}</button>
          </div>
        </section>)}
      {plan && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('out.plansteps')}>
          <h2>{plan.id} · {t(plan.mode === 'isolate' ? 'out.plan' : 'out.restore')} <span className="tag">{plan.data.status === 'ok' ? `● ${t('out.safe')}` : `▲ ${t('out.review')}`}</span></h2>
          <div className="tablewrap" style={{ maxHeight: 260 }}>
            <table><thead><tr><th>{t('out.step')}</th><th>{t('out.action')}</th><th>{t('out.check')}</th></tr></thead>
              <tbody>{plan.data.steps.map(s => (
                <tr key={s.seq}><td className="num">{s.seq}</td>
                  <td>{s.manual ? <span className="muted">{t('out.manual')}: {s.text}</span> : <><strong>{s.action?.toUpperCase()}</strong> {s.name} <span className="muted">{s.substation} {s.kv} kV</span>{s.interlock ? <div className="muted">{s.interlock}</div> : null}</>}</td>
                  <td className="num">{s.manual ? '–' : <>{s.safe ? '● ' : '▲ '}{t('out.maxload')} {n1(s.check!.max_loading_pct, 0)} %{s.check!.islanded_load_mw ? ` · ${t('out.islanded')} ${n1(s.check!.islanded_load_mw, 1)} MW` : ''}{s.check!.new_overloads.length ? ` · ${s.check!.new_overloads[0].branch} ${n1(s.check!.new_overloads[0].loading_pct, 0)} %` : ''}</>}</td></tr>))}</tbody></table>
          </div>
          <p className="muted num" style={{ fontSize: 12 }}>{t('out.hash')}: {plan.data.plan_hash}</p>
        </section>)}
      <p className="muted" style={{ fontSize: 12 }}>{t('out.footer')}</p>
    </div>
  );
}
