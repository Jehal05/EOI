import { useEffect, useMemo, useRef, useState } from 'react';
import { atLeast, getSession, post } from '../lib/api';
import { useLive, type AlarmRec } from '../lib/store';
import { t, type Key } from '../lib/i18n';
import { clockMs } from '../lib/fmt';
import { Sev } from './Icons';

export function sortAlarms(a: AlarmRec[]): AlarmRec[] {
  const un = (x: AlarmRec) => (x.state === 'ACTIVE_UNACK' || x.state === 'RTN_UNACK' ? 0 : 1);
  return [...a].sort((x, y) => un(x) - un(y) || x.prio - y.prio || y.raised - x.raised);
}

export function ShelveDialog({ tag, onDone }: { tag: string; onDone: () => void }) {
  const [reason, setReason] = useState(''), [min, setMin] = useState(30), [err, setErr] = useState('');
  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await post('/api/alarms/shelve', { tag, minutes: min, reason }); onDone(); } catch (x) { setErr((x as Error).message); }
  };
  return (
    <div className="overlay" onKeyDown={e => e.key === 'Escape' && onDone()}>
      <form className="dialog" onSubmit={go} role="dialog" aria-modal="true" aria-labelledby="sh">
        <h3 id="sh">{t('shelve.title')}</h3>
        <div className="mono muted">{tag}</div>
        <div className="field"><label htmlFor="sr">{t('shelve.reason')}</label><input id="sr" type="text" autoFocus value={reason} onChange={e => setReason(e.target.value)} required minLength={3} /></div>
        <div className="field"><label htmlFor="sm">{t('shelve.minutes')}</label><input id="sm" type="text" inputMode="numeric" value={min} onChange={e => setMin(Math.max(1, Math.min(1440, parseInt(e.target.value || '1', 10) || 1)))} /></div>
        <div className="muted" style={{ fontSize: 12 }}>{t('shelve.note')}</div>
        {err && <div className="err" role="alert">{err}</div>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" className="btn" onClick={onDone}>{t('common.cancel')}</button>
          <button className="btn primary" disabled={reason.trim().length < 3}>{t('alarm.shelve')}</button>
        </div>
      </form>
    </div>
  );
}

export function AlarmView() {
  const live = useLive();
  const s = getSession();
  const canOp = atLeast(s, 'operator');
  const [q, setQ] = useState(''), [unackOnly, setUnack] = useState(false), [prios, setPrios] = useState<Set<number>>(new Set([1, 2, 3, 4]));
  const [sel, setSel] = useState<number | null>(null), [shelve, setShelve] = useState<string | null>(null), [msg, setMsg] = useState('');
  const box = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => sortAlarms(live.alarms).filter(a =>
    prios.has(a.prio) && (!unackOnly || a.state === 'ACTIVE_UNACK' || a.state === 'RTN_UNACK') &&
    (!q || (a.text + a.tag + a.sub + a.type).toLowerCase().includes(q.toLowerCase()))), [live.alarms, q, unackOnly, prios]);

  const act = async (fn: () => Promise<unknown>) => { setMsg(''); try { await fn(); } catch (e) { setMsg((e as Error).message); } };
  const ack = (id: number) => act(() => post('/api/alarms/ack', { id }));
  const ackAll = () => act(() => post('/api/alarms/ack', { all: true }));
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === 'INPUT') return;
    const i = rows.findIndex(r => r.id === sel);
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel(rows[Math.min(rows.length - 1, i + 1)]?.id ?? null); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel(rows[Math.max(0, i - 1)]?.id ?? null); }
    else if (canOp && (e.key === 'a' || e.key === 'A')) { e.preventDefault(); if (e.shiftKey) void ackAll(); else if (sel) void ack(sel); }
    else if (canOp && (e.key === 's' || e.key === 'S')) { const r = rows.find(x => x.id === sel); if (r) setShelve(r.tag); }
  };
  useEffect(() => { if (sel && !rows.some(r => r.id === sel)) setSel(null); }, [rows, sel]);

  const toggle = (p: number) => setPrios(o => { const n = new Set(o); if (n.has(p)) n.delete(p); else n.add(p); return n; });
  return (
    <div onKeyDown={onKey} ref={box}>
      <h1>{t('nav.alarms')}
        <span className="chip" aria-live="polite">{live.stats ? `${live.stats.active} · ${live.stats.unacked} ${t('alarm.unackedOnly').toLowerCase()}` : '…'}</span>
        {!live.leader && <span className="chip warn">{t('top.standby')}</span>}
      </h1>
      <div className="row" style={{ marginBottom: 10 }}>
        <input type="search" placeholder={t('alarm.search')} aria-label={t('alarm.search')} value={q} onChange={e => setQ(e.target.value)} style={{ width: 260 }} />
        <div className="seg" role="group">{[1, 2, 3, 4].map(p => <button key={p} aria-pressed={prios.has(p)} onClick={() => toggle(p)}><Sev p={p} label={false} /> P{p}</button>)}</div>
        <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={unackOnly} onChange={e => setUnack(e.target.checked)} /> {t('alarm.unackedOnly')}</label>
        <span className="spacer" />
        {canOp && <button className="btn primary" onClick={() => void ackAll()} disabled={!live.stats?.unacked}>{t('alarm.ackAll')}</button>}
      </div>
      {msg && <div className="err" role="alert" style={{ marginBottom: 8 }}>{msg}</div>}
      <div className="tablewrap" tabIndex={0} aria-label={t('nav.alarms')}>
        {rows.length === 0 ? <div className="empty">{live.connected ? t('alarm.none') : t('common.loading')}</div> : (
          <table>
            <thead><tr><th style={{ width: 130 }}>P</th><th>{t('alarm.time')}</th><th>{t('alarm.station')}</th><th>{t('alarm.desc')}</th><th>{t('alarm.state')}</th><th /></tr></thead>
            <tbody>
              {rows.map(a => (
                <tr key={a.id} aria-selected={sel === a.id} className={a.state === 'ACTIVE_UNACK' || a.state === 'RTN_UNACK' ? 'unack' : ''} onClick={() => setSel(a.id)}>
                  <td><Sev p={a.prio} /></td>
                  <td className="num">{clockMs(a.raised)}</td>
                  <td>{a.sub.replace('SS-', 'S')}</td>
                  <td>{a.text} {a.firstOut && <span className="tag fo">{t('alarm.firstOut')}</span>} {a.consequential && <span className="tag cq">{t('alarm.consequential')}</span>} {a.shelved && <span className="tag">{t('alarm.shelved')}</span>}</td>
                  <td className="muted">{t(`state.${a.state}` as Key)}{a.ackBy ? ` · ${a.ackBy}` : ''}</td>
                  <td className="num">{canOp && <>
                    {(a.state === 'ACTIVE_UNACK' || a.state === 'RTN_UNACK') && <button className="btn sm" onClick={e => { e.stopPropagation(); void ack(a.id); }}>{t('alarm.ack')}</button>}{' '}
                    {a.shelved ? <button className="btn sm" onClick={e => { e.stopPropagation(); void act(() => post('/api/alarms/unshelve', { tag: a.tag })); }}>{t('alarm.unshelve')}</button>
                      : <button className="btn sm" onClick={e => { e.stopPropagation(); setShelve(a.tag); }}>{t('alarm.shelve')}</button>}</>}</td>
                </tr>))}
            </tbody>
          </table>)}
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>{t('alarm.hint')}</div>
      {shelve && <ShelveDialog tag={shelve} onDone={() => setShelve(null)} />}
    </div>
  );
}

/** Persistent strip: the most important unacknowledged alarms are always in view, whatever screen is open. */
export function AlarmBar({ onOpen }: { onOpen: () => void }) {
  const live = useLive();
  const canOp = atLeast(getSession(), 'operator');
  const top = sortAlarms(live.alarms.filter(a => !a.shelved && (a.state === 'ACTIVE_UNACK' || a.state === 'RTN_UNACK'))).slice(0, 3);
  const more = live.alarms.filter(a => !a.shelved && (a.state === 'ACTIVE_UNACK' || a.state === 'RTN_UNACK')).length - top.length;
  const newest = top.find(a => a.prio === 1);
  return (
    <div className="alarmbar" role="region" aria-label={t('nav.alarms')}>
      <span className="sr-only" aria-live="assertive">{newest ? `${t('prio.1')}: ${newest.text}` : ''}</span>
      {top.length === 0 && <span className="none">{t('alarm.none')}</span>}
      {top.map(a => (
        <div key={a.id} className={`banner p${a.prio}`}>
          <Sev p={a.prio} label={false} /><span className="t" title={a.text}>{a.text}</span>
          {canOp && <button className="btn sm" onClick={() => void post('/api/alarms/ack', { id: a.id }).catch(() => undefined)}>{t('alarm.ack')}</button>}
        </div>))}
      {more > 0 && <button className="btn sm" onClick={onOpen}>+{more}</button>}
    </div>
  );
}
