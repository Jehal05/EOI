export type Role = 'viewer' | 'operator' | 'engineer' | 'admin';
export interface Session { token: string; user: string; role: Role; jur: string[]; exp: number }

const RANK: Record<Role, number> = { viewer: 1, operator: 2, engineer: 3, admin: 4 };
export const atLeast = (s: Session | null, r: Role) => !!s && RANK[s.role] >= RANK[r];

let session: Session | null = null;
let onExpire: (() => void) | null = null;   // token lives in memory only: a reload requires signing in again
export const getSession = () => session;
export const expire = () => { onExpire?.(); };
export const setOnExpire = (f: () => void) => { onExpire = f; };
export function logout() { session = null; }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function login(user: string, password: string): Promise<Session> {
  const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user, password }) });
  if (!r.ok) throw new ApiError(r.status, 'login failed');
  const j = await r.json();
  session = { token: j.token, user: j.user, role: j.role, jur: j.jur, exp: j.exp };
  return session;
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  if (!session) throw new ApiError(401, 'not signed in');
  const headers = new Headers(init.headers);
  headers.set('Authorization', 'Bearer ' + session.token);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const r = await fetch(path, { ...init, headers });
  if (r.status === 401) { onExpire?.(); throw new ApiError(401, 'session expired'); }
  const text = await r.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* non-JSON error body */ }
  if (!r.ok) throw new ApiError(r.status, (body as { error?: string; detail?: string } | null)?.error ?? (body as { detail?: string } | null)?.detail ?? r.statusText);
  return body as T;
}

export const post = <T = unknown>(path: string, data: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(data) });

/** Raw text request/response (CIMXML and legacy files). The token stays in memory; nothing is written to storage. */
export async function apiText(path: string, init: RequestInit = {}): Promise<string> {
  if (!session) throw new ApiError(401, 'not signed in');
  const headers = new Headers(init.headers);
  headers.set('Authorization', 'Bearer ' + session.token);
  const r = await fetch(path, { ...init, headers });
  if (r.status === 401) { onExpire?.(); throw new ApiError(401, 'session expired'); }
  const text = await r.text();
  if (!r.ok) { let m = r.statusText; try { const j = JSON.parse(text); m = j.error ?? j.detail ?? m; } catch { /* not JSON */ } throw new ApiError(r.status, m); }
  return text;
}
