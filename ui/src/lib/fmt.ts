const tf = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
export const clock = (ms: number) => tf.format(ms);
export const clockMs = (ms: number) => `${tf.format(ms)}.${String(ms % 1000).padStart(3, '0')}`;
export const dateTime = (ms: number) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(ms);
export const n1 = (v: number | undefined | null, d = 1) => (v === undefined || v === null || Number.isNaN(v) ? '—' : v.toFixed(d));
export const shortHash = (h: string) => (h ? h.slice(0, 10) + '…' : '');
