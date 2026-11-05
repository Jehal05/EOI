import { useMemo, useState } from 'react';
import type { Model } from '../lib/model';
import { buildSld, type Bay } from '../lib/sld';
import { useLive } from '../lib/store';
import { ApiError, atLeast, getSession, post } from '../lib/api';
import { t } from '../lib/i18n';
import { n1 } from '../lib/fmt';

export function CommandDialog({ bay, onDone }: { bay: Bay; onDone: () => void }) {
  const live = useLive();
  const s = getSession();
  const pos = live.pts.get(bay.brkTag)?.s;
  const action = pos === 2 ? 'open' : 'close';
  const [reason, setReason] = useState(''), [code, setCode] = useState(''), [err, setErr] = useState(''), [ok, setOk] = useState(false), [busy, setBusy] = useState(false);
  const allowed = atLeast(s, 'operator');
  const go = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('');
    try { await post('/api/command', { tag: bay.brkTag, action, reason, totp: code }); setOk(true); setTimeout(onDone, 1600); }
    catch (x) { setErr(x instanceof ApiError ? x.message : t('common.error')); setCode(''); }
    finally { setBusy(false); }
  };
  return (
    <div className="overlay" onKeyDown={e => e.key === 'Escape' && onDone()}>
      <form className="dialog" onSubmit={go} role="dialog" aria-modal="true" aria-labelledby="ct">
        <h3 id="ct">{t('cmd.title')}</h3>
        <div><strong>{bay.brk}</strong> <span className="muted">· {bay.name}</span></div>
        <div className="muted" style={{ margin: '4px 0 8px' }}>{t('cmd.pos')}: <strong>{pos === 1 ? t('sld.open') : pos === 2 ? t('sld.closed') : t('sld.unknown')}</strong></div>
        {!allowed ? <div className="err">{t('cmd.noperm')}</div> : (<>
          <div className="field"><label htmlFor="cr">{t('cmd.reason')}</label><input id="cr" type="text" autoFocus value={reason} onChange={e => setReason(e.target.value)} maxLength={200} required minLength={3} /></div>
          <div className="field"><label htmlFor="cc">{t('cmd.totp')}</label><input id="cc" type="text" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} className="mono" value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} required /></div>
          <div className="muted" style={{ fontSize: 12 }}>{t('cmd.audit')}</div>
        </>)}
        {err && <div className="err" role="alert">{err}</div>}
        {ok && <div className="okmsg" role="status">{t('cmd.success')}</div>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" className="btn" onClick={onDone}>{t('common.cancel')}</button>
          {allowed && pos !== undefined && (pos === 1 || pos === 2) && <button className={`btn ${action === 'open' ? 'danger' : 'primary'}`} disabled={busy || ok || reason.trim().length < 3 || code.length !== 6}>{t(action === 'open' ? 'cmd.open' : 'cmd.close')} · {t('cmd.execute')}</button>}
        </div>
      </form>
    </div>
  );
}

function Symbol({ kind, y }: { kind: Bay['kind']; y: number }) {
  const c = { stroke: 'var(--text)', fill: 'none', strokeWidth: 1.6 };
  switch (kind) {
    case 'gen': return <g><circle cx="0" cy={y + 16} r="16" {...c} /><text x="0" y={y + 20} textAnchor="middle">G</text></g>;
    case 'transformer': return <g><circle cx="-7" cy={y + 14} r="11" {...c} /><circle cx="7" cy={y + 14} r="11" {...c} /></g>;
    case 'load': return <path d={`M-9 ${y + 4}h18l-9 16z`} {...c} />;
    case 'shunt': return <g><path d={`M-10 ${y + 6}h20M-10 ${y + 12}h20`} {...c} /><path d={`M0 ${y + 12}v10M-6 ${y + 22}h12`} {...c} /></g>;
    default: return <path d={`M-8 ${y + 18}l8 -14l8 14`} {...c} />;
  }
}

export function SldView({ model, sub, setSub }: { model: Model | null; sub: string; setSub: (s: string) => void }) {
  const live = useLive();
  const sld = useMemo(() => (model ? buildSld(model, sub) : null), [model, sub]);
  const [bay, setBay] = useState<Bay | null>(null);
  if (!model) return <div className="empty">{t('map.loading')}</div>;
  const subs = [...model.subs.values()];
  const v = sld ? live.pts.get(sld.busTag)?.v : undefined;
  const alarmTags = new Set(live.alarms.filter(a => a.state !== 'CLOSED' && !a.shelved).map(a => a.tag));
  const BUS_Y = 90;
  return (
    <div>
      <h1>{t('sld.title')}</h1>
      <div className="row" style={{ marginBottom: 10 }}>
        <label htmlFor="sub" className="muted">{t('sld.choose')}</label>
        <select id="sub" value={sub} onChange={e => setSub(e.target.value)} style={{ minWidth: 220 }}>{subs.map(s => <option key={s.id} value={s.id}>{s.name} ({s.region})</option>)}</select>
        <span className="muted">{t('sld.click')}</span>
      </div>
      {!sld ? <div className="empty">{t('common.none')}</div> : (
        <div className="card" style={{ overflow: 'auto' }}>
          <svg className="sld" width={sld.width} height={420} viewBox={`0 0 ${sld.width} 420`} role="group" aria-label={`${t('sld.title')} ${sld.name}`}>
            <line x1="20" y1={BUS_Y} x2={sld.width - 20} y2={BUS_Y} stroke="var(--text)" strokeWidth="5" strokeLinecap="round" />
            <text x="22" y={BUS_Y - 14} className="lbl">{t('sld.busbar')} · {sld.kv} kV</text>
            <text x={sld.width - 22} y={BUS_Y - 14} textAnchor="end">{v === undefined ? '—' : `${n1(v, 1)} kV · ${n1(v / sld.kv, 3)} pu`}</text>
            {sld.bays.map(b => {
              const pos = live.pts.get(b.brkTag)?.s;
              const p = b.pTag ? live.pts.get(b.pTag)?.v : undefined, q = b.qTag ? live.pts.get(b.qTag)?.v : undefined;
              const open = pos === 1, closed = pos === 2;
              const alarmed = [b.brkTag, b.pTag, b.qTag].some(x => x && alarmTags.has(x));
              return (
                <g key={b.brk} className="bay" transform={`translate(${b.x} 0)`} tabIndex={0} role="button" aria-label={`${b.brk} ${open ? t('sld.open') : closed ? t('sld.closed') : t('sld.unknown')}`}
                  onClick={() => setBay(b)} onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && setBay(b)}>
                  <line x1="0" y1={BUS_Y} x2="0" y2={BUS_Y + 30} stroke="var(--text)" strokeWidth="2" />
                  <rect className="brk" x="-9" y={BUS_Y + 30} width="18" height="18" fill={closed ? 'var(--text)' : 'var(--panel)'} stroke={alarmed ? 'var(--p1)' : 'var(--text)'} strokeWidth="2" strokeDasharray={pos === undefined || pos === 0 || pos === 3 ? '3 2' : undefined} />
                  {open && <path d={`M-9 ${BUS_Y + 48}L9 ${BUS_Y + 30}`} stroke="var(--p2)" strokeWidth="2.4" />}
                  <line x1="0" y1={BUS_Y + 48} x2="0" y2={BUS_Y + 100} stroke={open ? 'var(--grid-dead)' : 'var(--text)'} strokeWidth="2" strokeDasharray={open ? '4 3' : undefined} />
                  <text x="14" y={BUS_Y + 43} className="lbl">{open ? t('sld.open') : closed ? t('sld.closed') : t('sld.unknown')}</text>
                  <g transform={`translate(0 ${BUS_Y + 100 - 4})`}><Symbol kind={b.kind} y={0} /></g>
                  <text x="0" y={BUS_Y + 160} textAnchor="middle">{b.id}</text>
                  <text x="0" y={BUS_Y + 174} textAnchor="middle" className="lbl">{b.far ? `→ ${b.far.replace('Substation ', 'S')}` : b.kind}</text>
                  <text x="0" y={BUS_Y + 196} textAnchor="middle">{p === undefined ? '' : `${n1(p)} MW`}</text>
                  <text x="0" y={BUS_Y + 210} textAnchor="middle">{q === undefined ? '' : `${n1(q)} MVAr`}</text>
                  {alarmed && <path d={`M-30 ${BUS_Y + 125}l6 -10l6 10z`} fill="var(--p1)" />}
                </g>);
            })}
          </svg>
        </div>)}
      {bay && <CommandDialog bay={bay} onDone={() => setBay(null)} />}
    </div>
  );
}
