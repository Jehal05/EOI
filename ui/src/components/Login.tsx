import { useState } from 'react';
import { login } from '../lib/api';
import { t } from '../lib/i18n';

export function Login({ onDone }: { onDone: () => void }) {
  const [u, setU] = useState(''), [p, setP] = useState(''), [err, setErr] = useState(false), [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(false);
    try { await login(u.trim(), p); onDone(); } catch { setErr(true); setP(''); } finally { setBusy(false); }
  };
  return (
    <main className="login">
      <form className="dialog" onSubmit={submit} aria-labelledby="lt">
        <div className="brand" style={{ marginBottom: 14 }}>
          <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--panel2)" /><path d="M17 4 8 18h7l-1 10 10-15h-7z" fill="var(--accent)" /></svg>
          <span>{t('app.title')}</span>
        </div>
        <h3 id="lt">{t('login.title')}</h3>
        <div className="field"><label htmlFor="u">{t('login.user')}</label><input id="u" type="text" autoComplete="username" autoFocus value={u} onChange={e => setU(e.target.value)} required /></div>
        <div className="field"><label htmlFor="p">{t('login.password')}</label><input id="p" type="password" autoComplete="current-password" value={p} onChange={e => setP(e.target.value)} required /></div>
        {err && <div className="err" role="alert">{t('login.failed')}</div>}
        <div className="row" style={{ marginTop: 14, justifyContent: 'space-between' }}>
          <span className="muted" style={{ fontSize: 12, maxWidth: 220 }}>{t('login.notice')}</span>
          <button className="btn primary" disabled={busy || !u || !p}>{t('login.submit')}</button>
        </div>
      </form>
    </main>
  );
}
