import { useEffect, useState } from 'react';
import { api, post } from '../lib/api';
import { t, type Key } from '../lib/i18n';
import { n1 } from '../lib/fmt';

interface Scen { id: string; name: string; description: string; duration: number }
interface Viol { kind: string; id: string; value: number }
interface St { t: number; secure: boolean; violations: Viol[]; n_violations: number; max_loading: number; top_loading: { id: string; pct: number }[]; lost_load_mw: number; out_branches: string[]; gens_off: string[] }
interface Log { t: number; kind: string; text: string }
interface Score { total: number; security_time: number; final_state: number; load_kept: number; action_quality: number; insecure_s: number; actions: number; harmful: number; refused: number }
const ACTS = ['open_branch', 'close_branch', 'set_gen', 'shed_load'];
const MARK: Record<string, string> = { event: '■', alarm: '▲', clear: '●', action: '▶' };

export function DtsView({ model }: { model: string }) {
  const [scs, setScs] = useState<Scen[]>([]); const [sel, setSel] = useState('');
  const [sid, setSid] = useState(''); const [sc, setSc] = useState<Scen | null>(null);
  const [st, setSt] = useState<St | null>(null); const [log, setLog] = useState<Log[]>([]); const [score, setScore] = useState<Score | null>(null);
  const [act, setAct] = useState(ACTS[0]); const [target, setTarget] = useState(''); const [val, setVal] = useState(0); const [msg, setMsg] = useState(''); const [busy, setBusy] = useState(false);
  const [cat, setCat] = useState<string[]>([]);
  useEffect(() => { api<{ scenarios: Scen[] }>(`/ems/v1/dts/scenarios?model=${model}`).then(r => { setScs(r.scenarios); setSel(r.scenarios[0]?.id ?? ''); }).catch(() => undefined); }, [model]);
  useEffect(() => { api<{ acLineSegments: { mrid: string }[]; transformers: { mrid: string }[] }>(`/cim/models/${model}`).then(m => setCat([...m.acLineSegments, ...m.transformers].map(e => e.mrid))).catch(() => undefined); }, [model]);
  const start = () => { setBusy(true); setMsg(''); setScore(null); post<{ id: string; scenario: Scen; state: St }>('/ems/v1/dts/sessions', { model, scenario: sel }).then(r => { setSid(r.id); setSc(r.scenario); setSt(r.state); setLog([]); }).catch(e => setMsg((e as Error).message)).finally(() => setBusy(false)); };
  const step = (dt: number) => { setBusy(true); post<{ state: St; log: Log[] }>(`/ems/v1/dts/sessions/${sid}/step`, { dt }).then(r => { setSt(r.state); setLog(r.log); }).catch(e => setMsg((e as Error).message)).finally(() => setBusy(false)); };
  const doAct = () => { setBusy(true); setMsg(''); post<{ ok: boolean; message: string; state: St; log: Log[] }>(`/ems/v1/dts/sessions/${sid}/action`, { action: act, target, value: act === 'set_gen' || act === 'shed_load' ? val : undefined }).then(r => { setSt(r.state); setLog(r.log); if (!r.ok) setMsg(r.message); }).catch(e => setMsg((e as Error).message)).finally(() => setBusy(false)); };
  const end = () => post<{ score: Score; log: Log[] }>(`/ems/v1/dts/sessions/${sid}/end`, {}).then(r => { setScore(r.score); setLog(r.log); setSid(''); }).catch(e => setMsg((e as Error).message));
  const done = !!sc && !!st && st.t >= sc.duration;
  return (
    <div>
      <h1>{t('dts.title')}</h1>
      <div role="note" className="card" style={{ borderLeft: '6px solid var(--accent)', marginBottom: 10 }}><strong>■ {t('dts.banner')}</strong></div>
      {!sid && (
        <section className="card" style={{ marginBottom: 12 }} aria-label={t('dts.scen')}>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label>{t('dts.scen')} <select value={sel} onChange={e => setSel(e.target.value)}>{scs.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
            <button className="btn primary" onClick={start} disabled={busy || !sel}>{t('dts.start')}</button>
          </div>
          <p className="muted">{scs.find(s => s.id === sel)?.description}</p>
        </section>)}
      {msg && <div role="alert" className="errmsg">{msg}</div>}
      {sid && sc && st && (<>
        <p><strong>{sc.name}</strong> · {t('dts.t')}: <span className="num">{n1(st.t, 0)} / {sc.duration} s</span> · <span className="tag" role="status">{st.secure ? `● ${t('dts.secure')}` : `▲ ${t('dts.insecure')}`}</span> · {t('dts.maxload')}: <span className="num">{n1(st.max_loading, 0)} %</span> · {t('dts.lost')}: <span className="num">{n1(st.lost_load_mw, 1)} MW</span></p>
        <div className="row" style={{ gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
          <span>{t('dts.adv')}:</span>
          {[10, 30, 60, 300].map(d => <button key={d} className="btn" disabled={busy || done} onClick={() => step(d)}>{d} s</button>)}
          <button className="btn" disabled={busy || done} onClick={() => step(sc.duration - st.t)}>▶▶</button>
          <button className="btn primary" onClick={() => void end()}>{t('dts.end')}</button>
        </div>
        {done && <p className="muted">{t('dts.done')}</p>}
        <div className="row" style={{ gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <section className="card" style={{ flex: '1 1 320px' }} aria-label={t('dts.viol')}>
            <h2>{t('dts.viol')} ({st.n_violations})</h2>
            {!st.violations.length ? <p className="muted">{t('dts.none')}</p> : <ul>{st.violations.slice(0, 12).map(v => <li key={v.kind + v.id}>▲ {t(`dts.kind.${v.kind}` as Key)}: {v.id} <span className="num">{v.value}{v.kind === 'overload' ? ' %' : v.kind === 'voltage' ? ' pu' : ' MW'}</span></li>)}</ul>}
            <h3>{t('dts.top')}</h3>
            <ul>{st.top_loading.map(b => <li key={b.id} className="num">{b.id} {n1(b.pct, 0)} %{st.out_branches.includes(b.id) ? ' (out)' : ''}</li>)}</ul>
            <p className="muted">{t('dts.out')}: {st.out_branches.join(', ') || '–'}</p>
          </section>
          <section className="card" style={{ flex: '1 1 320px' }} aria-label={t('dts.act')}>
            <h2>{t('dts.act')}</h2>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
              <label>{t('dts.act')} <select value={act} onChange={e => setAct(e.target.value)}>{ACTS.map(a => <option key={a} value={a}>{t(`dts.a.${a}` as Key)}</option>)}</select></label>
              <label>{t('dts.target')} <input list="dtscat" value={target} onChange={e => setTarget(e.target.value)} style={{ width: 120 }} />
                <datalist id="dtscat">{act.endsWith('branch') ? cat.map(c => <option key={c} value={c} />) : null}</datalist></label>
              {(act === 'set_gen' || act === 'shed_load') && <label>{t('dts.value')} <input type="number" value={val} onChange={e => setVal(Number(e.target.value))} style={{ width: 90 }} /></label>}
              <button className="btn primary" disabled={busy || !target} onClick={doAct}>{t('dts.do')}</button>
            </div>
          </section>
        </div>
      </>)}
      {score && (
        <section className="card" style={{ marginTop: 12 }} aria-label={t('dts.score')}>
          <h2>{t('dts.score')}: <span className="num">{score.total} / 100</span></h2>
          <ul>{(['security_time', 'final_state', 'load_kept', 'action_quality'] as const).map(k => <li key={k}>{t(`dts.s.${k}` as Key)}: <span className="num">{score[k]}</span></li>)}</ul>
          <p className="muted num">{t('dts.refused')}: {score.refused} · {score.harmful} · {n1(score.insecure_s, 0)} s</p>
          <p className="muted">{t('dts.aid')}</p>
        </section>)}
      {log.length > 0 && (
        <section style={{ marginTop: 12 }} aria-label={t('dts.log')}><h2>{t('dts.log')}</h2>
          <div className="tablewrap" style={{ maxHeight: 220 }}><table><tbody>{[...log].reverse().map((l, i) => <tr key={i}><td className="num">{n1(l.t, 0)} s</td><td>{MARK[l.kind]} {l.text}</td></tr>)}</tbody></table></div></section>)}
    </div>
  );
}
