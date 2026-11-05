import { useSyncExternalStore } from 'react';
import { expire, getSession } from './api';

export interface Pt { v?: number; s?: number; q?: string; m: number; n: number }
export interface AlarmRec {
  id: number; tag: string; type: string; prio: number; text: string; sub: string; state: string; raised: number;
  ackBy?: string; group?: number; firstOut?: boolean; consequential?: boolean; shelved?: boolean; v?: number; rtn?: number;
}
export interface AlarmStats { active: number; unacked: number; shelved: number; p1: number; p2: number; p3: number; p4: number; raised_last_10min: number; peak_10min: number }
export interface Live {
  pts: Map<string, Pt>; alarms: AlarmRec[]; stats: AlarmStats | null;
  connected: boolean; leader: boolean; lastMsg: number; seq: number; version: number; signedIn: boolean;
}

const live: Live = { pts: new Map(), alarms: [], stats: null, connected: false, leader: true, lastMsg: 0, seq: 0, version: 0, signedIn: false };
const subs = new Set<() => void>();
let snapshot: Live = { ...live };
let scheduled = false;

/** Coalesce notifications to ~10 Hz so a burst of telemetry costs one render, not hundreds. */
function notify() {
  if (scheduled) return;
  scheduled = true;
  setTimeout(() => { scheduled = false; live.version++; snapshot = { ...live }; subs.forEach(f => f()); }, 100);
}
export const subscribe = (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; };
export const useLive = (): Live => useSyncExternalStore(subscribe, () => snapshot);

export function applyMessage(m: { seq?: number; leader?: boolean; pts?: Array<{ t: string; v?: number; s?: number; q?: string; m: number; n: number }>; alarms?: AlarmRec[]; stats?: AlarmStats }) {
  if (m.pts) for (const p of m.pts) live.pts.set(p.t, { v: p.v, s: p.s, q: p.q, m: p.m, n: p.n });
  if (m.alarms) live.alarms = m.alarms;
  if (m.stats) live.stats = m.stats;
  if (m.seq !== undefined) live.seq = m.seq;
  if (m.leader !== undefined) live.leader = m.leader;
  live.lastMsg = Date.now();
  live.connected = true;
  notify();
}

let abort: AbortController | null = null;
let watchdog: ReturnType<typeof setInterval> | null = null;

export function startStream() {
  stopStream();
  live.signedIn = true;
  const ac = new AbortController();
  abort = ac;
  watchdog = setInterval(() => {   // the server sends a heartbeat at least every 5 s
    if (live.connected && Date.now() - live.lastMsg > 9000) { live.connected = false; notify(); ac.abort(); startStreamLoop(); }
  }, 2000);
  void streamLoop(ac);
}
function startStreamLoop() { if (getSession()) { abort = new AbortController(); void streamLoop(abort); } }

async function streamLoop(ac: AbortController) {
  let backoff = 500;
  while (!ac.signal.aborted && getSession()) {
    try {
      const r = await fetch('/api/stream', { headers: { Authorization: 'Bearer ' + getSession()!.token }, signal: ac.signal });
      if (r.status === 401) { expire(); return; }
      if (!r.ok || !r.body) throw new Error('stream ' + r.status);
      backoff = 500;
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 1);
          if (line) applyMessage(JSON.parse(line));
        }
      }
    } catch { /* fall through to reconnect */ }
    if (ac.signal.aborted) return;
    live.connected = false; notify();
    await new Promise(r => setTimeout(r, backoff));
    backoff = Math.min(backoff * 2, 5000);
  }
}

export function stopStream() {
  abort?.abort(); abort = null;
  if (watchdog) clearInterval(watchdog);
  watchdog = null;
  live.pts = new Map(); live.alarms = []; live.stats = null; live.connected = false; live.signedIn = false;
}
