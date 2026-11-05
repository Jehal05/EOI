import { useCallback, useEffect, useMemo, useState } from 'react';
import { getSession, logout, setOnExpire, type Session } from '../lib/api';
import { applyDim, applyTheme, loadPrefs, savePref, THEMES, type ThemeName } from '../lib/theme';
import { setLang, t, type Key, type Lang } from '../lib/i18n';
import { startStream, stopStream, useLive } from '../lib/store';
import { loadModel, type Model } from '../lib/model';
import { clock } from '../lib/fmt';
import { useNow } from '../lib/hooks';
import { Login } from './Login';
import { NavIcon, Sev } from './Icons';
import { MapView } from './MapView';
import { AlarmBar, AlarmView } from './AlarmView';
import { SldView } from './SldView';
import { ContingencyView, SeView } from './Analysis';
import { ShortCircuitView } from './ShortCircuit';
import { DispatchView } from './Dispatch';
import { SecurityView } from './Security';
import { TcmView } from './Tcm';
import { ForecastView } from './Forecast';
import { AgcView } from './Agc';
import { DsaView } from './Dsa';
import { OutagesView } from './Outages';
import { LogbookView } from './Logbook';
import { ShedView } from './Shed';
import { DtsView } from './Dts';
import { GovernanceView } from './Governance';
import { InteropView } from './Interop';
import { TrendView } from './TrendView';
import { HealthView, ModelView, SoeView } from './System';

type View = 'map' | 'alarms' | 'sld' | 'soe' | 'contingency' | 'shortcircuit' | 'dispatch' | 'security' | 'tcm' | 'forecast' | 'agc' | 'dsa' | 'outages' | 'logbook' | 'shed' | 'dts' | 'governance' | 'interop' | 'se' | 'trends' | 'health' | 'model';
const NAV: [string, View[]][] = [['nav.operations', ['map', 'alarms', 'sld', 'soe']], ['nav.analysis', ['contingency', 'shortcircuit', 'dispatch', 'security', 'tcm', 'forecast', 'agc', 'dsa', 'outages', 'shed', 'dts', 'logbook', 'se', 'trends']], ['nav.system', ['health', 'model', 'governance', 'interop']]];
const MODEL = 'ieee118';

export function App() {
  const [session, setSession] = useState<Session | null>(getSession());
  useEffect(() => { setOnExpire(() => { stopStream(); logout(); setSession(null); }); }, []);   // any 401 returns to sign-in
  if (!session) return <LoginGate onDone={() => { setSession(getSession()); startStream(); }} />;
  return <Shell session={session} onLogout={() => { stopStream(); logout(); setSession(null); }} />;
}

function LoginGate({ onDone }: { onDone: () => void }) {
  useEffect(() => { applyTheme('night'); applyDim(false); setLang('en'); }, []);
  return <Login onDone={onDone} />;
}

function Shell({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const live = useLive();
  const prefs = useMemo(() => loadPrefs(session.user), [session.user]);
  const [theme, setTheme] = useState<ThemeName>(prefs.theme), [dim, setDim] = useState(prefs.dim), [lang, setLg] = useState<Lang>(prefs.lang);
  const [view, setView] = useState<View>('map'), [sub, setSub] = useState('SS-0010'), [model, setModel] = useState<Model | null>(null);
  const now = useNow(1000);
  setLang(lang);   // synchronous so every child renders in the chosen language
  useEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => { applyDim(dim); }, [dim]);
  useEffect(() => { loadModel(MODEL).then(setModel).catch(() => undefined); }, []);
  const pick = useCallback((k: 'theme' | 'dim' | 'lang', v: string) => savePref(session.user, k, v), [session.user]);
  const openSld = (s: string) => { setSub(s); setView('sld'); };
  const stale = !live.connected;
  const s = live.stats;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--panel2)" /><path d="M17 4 8 18h7l-1 10 10-15h-7z" fill="var(--accent)" /></svg><span>GridSovereign</span><small>SCADA/EMS</small></div>
        <span className={`chip ${live.connected ? '' : 'bad'}`}>{live.connected ? '● ' + t('top.live') : '✕ ' + t('top.offline')}</span>
        <span className={`chip ${live.leader ? '' : 'warn'}`}>{live.leader ? t('top.leader') : t('top.standby')}</span>
        <div className="counts" role="group" aria-label={t('nav.alarms')}>
          {[1, 2, 3].map(p => { const n = s ? (s as unknown as Record<string, number>)[`p${p}`] : 0; return <button key={p} className={`count p${p} ${n ? 'on' : ''}`} onClick={() => setView('alarms')} aria-label={`${t(`prio.${p}` as Key)}: ${n}`}><Sev p={p} label={false} /> <span className="num">{n}</span></button>; })}
        </div>
        <span className="spacer" />
        <span className="clock num" aria-label="IST">{clock(now)} IST</span>
        <div className="seg" role="group" aria-label={t('top.theme')}>{THEMES.map(th => <button key={th} aria-pressed={theme === th} onClick={() => { setTheme(th); pick('theme', th); }}>{t(`theme.${th}` as Key)}</button>)}</div>
        <button className="btn" aria-pressed={dim} title={t('top.dim')} onClick={() => { setDim(!dim); pick('dim', dim ? '0' : '1'); }}>◐</button>
        <div className="seg" role="group" aria-label={t('top.language')}>{(['en', 'hi'] as Lang[]).map(l => <button key={l} aria-pressed={lang === l} onClick={() => { setLg(l); pick('lang', l); }}>{l === 'en' ? 'EN' : 'हिं'}</button>)}</div>
        <span className="chip">{session.user} · {session.role}</span>
        <button className="btn" onClick={onLogout}>{t('top.signout')}</button>
      </header>
      <nav className="nav" aria-label="main">
        {NAV.map(([g, items]) => <div key={g} style={{ display: 'contents' }}><div className="grp">{t(g as Key)}</div>
          {items.map(v => <button key={v} aria-current={view === v ? 'page' : undefined} onClick={() => setView(v)}><NavIcon n={v} /><span className="t">{t(`nav.${v}` as Key)}</span>
            {v === 'alarms' && s && s.unacked > 0 && <span className="badge num">{s.unacked}</span>}</button>)}</div>)}
      </nav>
      <main className="main" id="main">
        {stale && <div className="stale" role="alert">{t('top.offline')}</div>}
        {view === 'map' && <MapView model={model} onOpenSld={openSld} />}
        {view === 'alarms' && <AlarmView />}
        {view === 'sld' && <SldView model={model} sub={sub} setSub={setSub} />}
        {view === 'soe' && <SoeView />}
        {view === 'contingency' && <ContingencyView />}
        {view === 'shortcircuit' && <ShortCircuitView model={MODEL} />}
        {view === 'dispatch' && <DispatchView model={MODEL} />}
        {view === 'security' && <SecurityView model={MODEL} />}
        {view === 'tcm' && <TcmView model={MODEL} />}
        {view === 'forecast' && <ForecastView />}
        {view === 'agc' && <AgcView model={MODEL} />}
        {view === 'dsa' && <DsaView model={MODEL} />}
        {view === 'outages' && <OutagesView model={MODEL} />}
        {view === 'dts' && <DtsView model={MODEL} />}
        {view === 'shed' && <ShedView model={MODEL} />}
        {view === 'logbook' && <LogbookView />}
        {view === 'se' && <SeView />}
        {view === 'trends' && <TrendView model={MODEL} />}
        {view === 'health' && <HealthView />}
        {view === 'model' && <ModelView model={MODEL} />}
        {view === 'governance' && <GovernanceView model={MODEL} />}
        {view === 'interop' && <InteropView model={MODEL} />}
      </main>
      <AlarmBar onOpen={() => setView('alarms')} />
    </div>
  );
}
