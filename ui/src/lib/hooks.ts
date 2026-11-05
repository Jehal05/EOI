import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';

/** Poll an authenticated endpoint. Keeps the last good value while refreshing; exposes loading/error for honest empty states. */
export function usePoll<T>(path: string | null, ms: number): { data: T | null; err: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    if (!path) { setLoading(false); return; }
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      try { const d = await api<T>(path); if (alive.current) { setData(d); setErr(null); } }
      catch (e) { if (alive.current) setErr((e as Error).message); }
      finally { if (alive.current) setLoading(false); }
      if (alive.current && ms > 0) timer = setTimeout(run, ms);
    };
    setLoading(true);
    void run();
    return () => { alive.current = false; clearTimeout(timer); };
  }, [path, ms, tick]);
  const reload = useCallback(() => setTick(x => x + 1), []);
  return { data, err, loading, reload };
}

export function useNow(ms = 1000): number {
  const [n, setN] = useState(Date.now());
  useEffect(() => { const i = setInterval(() => setN(Date.now()), ms); return () => clearInterval(i); }, [ms]);
  return n;
}
