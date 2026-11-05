import { useEffect, useRef, useState } from 'react';
import { api, atLeast, getSession } from '../lib/api';
import { usePoll } from '../lib/hooks';
import { t } from '../lib/i18n';
import { clockMs, dateTime, n1, shortHash } from '../lib/fmt';

interface Metrics {
  node: string; uptime_s: number; ingest_lines_accepted: number; queue_depth: number; dropped: number; applied: number; no_change_filtered: number;
  latency_ms_p50: number; latency_ms_p95: number; latency_ms_p99: number; latency_ms_max: number; points: number; log_seq: number; log_bytes?: number; degraded: boolean;
  commands: number; commands_failed: number; auth_failures: number;
  alarms: { raised_last_10min: number; peak_10min: number; active: number; unacked: number; shelved: number };
  cluster: { node: string; role: string; mode?: string; term?: number; leader?: string | null; quorum?: number; lease_ms?: number; step_downs?: number; stale_rejected?: number; peers: { id: string; alive: boolean }[]; failovers: number; replicated_ok: number; replicated_failed: number };
}
interface HistStats { backend?: string; raw_rows: number; agg1m_rows: number; tags: number; db_bytes: number; newest_ms: number | null; avg_rows_per_s: number }
interface Verify { chain_ok: boolean; identical: boolean; records: number; replay_ms: number; state_hash_live: string; state_hash_replay: string; alarm_hash_live: string }

function Kpi({ l, v, s, cls = '' }: { l: string; v: React.ReactNode; s?: React.ReactNode; cls?: string }) {
  return <div className="card kpi"><div className="l">{l}</div><div className={`v num ${cls}`}>{v}</div>{s && <div className="s">{s}</div>}</div>;
}

export function HealthView() {
  const { data: m } = usePoll<Metrics>('/api/metrics', 2000);
  const { data: hs } = usePoll<HistStats>('/hist/v1/stats', 10000);
  const [rate, setRate] = useState(0);
  const prev = useRef<{ t: number; n: number } | null>(null);
  const [ver, setVer] = useState<Verify | null>(null), [verr, setVerr] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!m) return;
    const now = Date.now();
    if (prev.current && now > prev.current.t) setRate(((m.ingest_lines_accepted - prev.current.n) / (now - prev.current.t)) * 1000);
    prev.current = { t: now, n: m.ingest_lines_accepted };
  }, [m]);
  const canVerify = atLeast(getSession(), 'engineer');
  const verify = async () => { setBusy(true); setVerr(''); try { setVer(await api<Verify>('/api/verify-replay')); } catch (e) { setVerr((e as Error).message); } finally { setBusy(false); } };
  if (!m) return <div className="empty"><div className="skeleton" style={{ width: 240, margin: '0 auto' }} /></div>;
  const flood = m.alarms.raised_last_10min;
  return (
    <div>
      <h1>{t('health.title')}</h1>
      <div className="grid g4">
        <Kpi l={t('health.node')} v={`${m.node} · ${m.cluster.role === 'leader' ? t('top.leader') : t('top.standby')}`} s={`${t('health.uptime')} ${Math.floor(m.uptime_s / 60)} min`} />
        <Kpi l={t('health.ingest')} v={`${n1(rate, 0)}/s`} s={`${t('health.points')}: ${m.points} · ${t('health.queue')}: ${m.queue_depth}`} />
        <Kpi l={t('health.latency')} v={`${m.latency_ms_p50} ms`} s={`p95 ${m.latency_ms_p95} · p99 ${m.latency_ms_p99} · max ${m.latency_ms_max}`} />
        <Kpi l={t('health.dropped')} v={m.dropped} cls={m.dropped ? 'bad' : ''} s={`${t('health.log')}: #${m.log_seq}${m.log_bytes ? ` · ${n1(m.log_bytes / 1048576, 1)} MB` : ''}`} />
      </div>
      <div className="grid g2" style={{ marginTop: 12 }}>
        <div className="card"><h2>{t('health.alarms')}</h2>
          <div className="row"><span className={`num ${flood > 10 ? 'warn' : ''}`} style={{ fontSize: 26 }}>{flood}</span><span className="muted">{t('health.flood')}</span></div>
          <div className="muted" style={{ fontSize: 12 }}>{t('health.floodNote')} · {t('health.peak')}: {m.alarms.peak_10min}</div>
        </div>
        <div className="card"><h2>{t('health.cluster')}</h2>
          <div className="row">{m.cluster.peers.map(p => <span key={p.id} className={`chip ${p.alive ? '' : 'bad'}`}>{p.id}: {p.alive ? '●' : '✕'} {p.alive ? 'up' : 'down'}</span>)}</div>
          <div className="muted" style={{ marginTop: 6 }}>
            {t('health.mode')}: {m.cluster.mode === 'quorum' ? t('health.quorumMode') : m.cluster.mode === 'pair' ? t('health.pairMode') : t('health.singleMode')}
            {m.cluster.mode === 'quorum' && <> · {t('health.term')}: {m.cluster.term} · {t('health.quorum')}: {m.cluster.quorum} · {t('health.leader')}: {m.cluster.leader ?? '—'} · {t('health.lease')}: {m.cluster.lease_ms} ms · {t('health.stepdowns')}: {m.cluster.step_downs} · {t('health.stale')}: {m.cluster.stale_rejected}</>}
          </div>
          <div className="muted" style={{ marginTop: 6 }}>{t('health.failovers')}: {m.cluster.failovers} · {t('health.replicated')}: {m.cluster.replicated_ok}{m.cluster.replicated_failed ? ` (${m.cluster.replicated_failed} ✕)` : ''}</div>
        </div>
        <div className="card"><h2>{t('health.hist')}</h2>
          {hs ? <div className="muted">{t('health.histBackend')}: <strong>{hs.backend ?? 'sqlite'}</strong> · {t('health.histRows')}: <span className="num">{hs.raw_rows.toLocaleString()}</span> · {t('health.histTags')}: <span className="num">{hs.tags}</span> · {Math.round(hs.db_bytes / 1048576)} MB · {t('health.histAge')}: <span className="num">{hs.newest_ms ? Math.max(0, Math.round((Date.now() - hs.newest_ms) / 1000)) : '—'} s</span></div> : <div className="muted">—</div>}
        </div>
        <div className="card"><h2>{t('health.protocols')}</h2>
          <div className="muted">{t('health.protoBuilt')}: <strong>IEC 60870-5-104 · IEC 60870-5-101 · DNP3 · IEEE C37.118.2 ({t('health.pmuRo')}) · OPC UA ({t('health.pmuRo')}, {t('health.opcNone')})</strong></div>
          <div className="muted" style={{ marginTop: 6 }}>{t('health.protoNot')}: <strong>ICCP/TASE.2 · IEC 61850 MMS</strong></div>
          <div className="muted" style={{ marginTop: 6 }}>{t('health.protoNote')}</div>
        </div>
        <div className="card"><h2>{t('health.cyber')}</h2>
          <div className="row"><span>{t('health.authfail')}: <strong className="num">{m.auth_failures}</strong></span><span>{t('health.cmds')}: <strong className="num">{m.commands}</strong></span><span>{t('health.cmdfail')}: <strong className="num">{m.commands_failed}</strong></span></div>
        </div>
        <div className="card"><h2>{t('health.verify')}</h2>
          {canVerify ? <button className="btn primary" onClick={() => void verify()} disabled={busy}>{t('health.verify')}</button> : <div className="muted">{t('health.engineer')}</div>}
          {verr && <div className="err">{verr}</div>}
          {ver && <div style={{ marginTop: 8 }} role="status"><span className={ver.identical && ver.chain_ok ? 'okmsg' : 'err'}>{ver.identical && ver.chain_ok ? '✔ ' + t('health.verifyOk') : '✖ ' + t('health.verifyBad')}</span>
            <div className="muted mono" style={{ fontSize: 12 }}>{ver.records} records · {ver.replay_ms} ms · {shortHash(ver.state_hash_live)} = {shortHash(ver.state_hash_replay)}</div></div>}
        </div>
      </div>
    </div>
  );
}

interface Soe { events: { seq: number; ts: number; src?: string; tag: string; sub: string; text: string }[] }
export function SoeView() {
  const { data } = usePoll<Soe>('/api/soe?limit=300', 3000);
  return (
    <div>
      <h1>{t('soe.title')}</h1>
      <div className="tablewrap">
        {!data || data.events.length === 0 ? <div className="empty">{t('common.none')}</div> : (
          <table><thead><tr><th>{t('soe.time')}</th><th>{t('soe.station')}</th><th>{t('soe.point')}</th><th>{t('soe.event')}</th></tr></thead>
            <tbody>{data.events.map(e => <tr key={e.seq}><td className="num">{clockMs(e.ts)}</td><td>{e.sub.replace('SS-', 'S')}</td><td className="mono">{e.tag}</td><td>{e.text}</td></tr>)}</tbody></table>)}
      </div>
    </div>
  );
}

interface ModelInfo { id: string; name: string; version: number; hash: string; substations: number; lines: number; transformers: number; generators: number; loads: number; switches: number }
interface Hist { version: number; ts_ms: number; user: string; action: string; detail: string; hash: string }
export function ModelView({ model }: { model: string }) {
  const { data: list } = usePoll<ModelInfo[]>('/cim/models', 0);
  const { data: hist } = usePoll<Hist[]>(`/cim/models/${model}/history`, 0);
  const { data: val } = usePoll<{ valid: boolean; issues: { code?: string; severity?: string; message?: string }[] }>(`/cim/models/${model}/validate`, 0);
  const [chain, setChain] = useState<string>('');
  const info = list?.find(x => x.id === model);
  return (
    <div>
      <h1>{t('model.title')}</h1>
      {info && <div className="grid g4" style={{ marginBottom: 12 }}>
        <div className="card kpi"><div className="l">{info.name}</div><div className="v num">v{info.version}</div><div className="s mono">{shortHash(info.hash)}</div></div>
        <div className="card kpi"><div className="l">{t('nav.map')}</div><div className="v num">{info.substations}</div><div className="s">{info.lines} lines · {info.transformers} xfmr</div></div>
        <div className="card kpi"><div className="l">CIM</div><div className="v num">{info.switches}</div><div className="s">{info.generators} gen · {info.loads} loads</div></div>
        <div className="card kpi"><div className="l">{t('model.validate')}</div><div className={`v ${val?.valid ? '' : 'bad'}`}>{val ? (val.valid ? '✔' : '✖') : '…'}</div><div className="s">{val ? (val.issues.length ? `${val.issues.length} ${t('model.issues')}` : t('model.clean')) : ''}</div></div>
      </div>}
      <div className="card">
        <div className="row" style={{ justifyContent: 'space-between' }}><h2>{t('model.history')}</h2>
          <span className="row">{chain && <span className="okmsg">{chain}</span>}<button className="btn" onClick={() => void api<{ ok: boolean; records: number }>(`/cim/models/${model}/verify`).then(r => setChain(r.ok ? `✔ ${t('model.verifyOk')} (${r.records})` : '✖')).catch(e => setChain((e as Error).message))}>{t('model.verify')}</button></span></div>
        <table><thead><tr><th>{t('model.version')}</th><th>{t('soe.time')}</th><th>{t('model.user')}</th><th>{t('model.action')}</th><th>{t('model.detail')}</th><th>{t('model.hash')}</th></tr></thead>
          <tbody>{[...(hist ?? [])].reverse().map(h => <tr key={h.version}><td className="num">{h.version}</td><td className="num">{dateTime(h.ts_ms)}</td><td>{h.user}</td><td>{h.action}</td><td>{h.detail}</td><td className="mono muted">{shortHash(h.hash)}</td></tr>)}</tbody></table>
      </div>
      <p className="muted" style={{ fontSize: 12 }}>{t('model.readonly')}</p>
    </div>
  );
}
